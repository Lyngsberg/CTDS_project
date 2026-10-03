"""Clean typed trips and build the canonical station table.

Every raw trip gets at most one `drop_reason` (first matching rule wins), so the
cleaning report is a waterfall that sums to the raw row count. Findings from
exploring 2026-08 that motivate the rules:

* Station ids are strings like '5980.10', but the source sometimes loses the
  trailing zero ('5980.1') or appends '_' ('5303.06_'). Both refer to the same
  station, so ids are normalised to two decimals (2474 raw -> 2390 stations).
* Coordinates are the station's own coordinates, even for e-bikes, so the
  station table can take the modal coordinate per station.
* Jersey City/Hoboken stations (JC*, HB*) show up on cross-Hudson rides, and
  operator stations (SYS*, 'Shop Morgan', and 'Morgan HCT Charging', which hides
  behind the numeric id '1234.56') on maintenance moves. Both are dropped.
* The source already removes trips under 60 s. Zip files are split by *end*
  time, so trips are assigned to the study window by `started_at`.
* Round trips (same start and end station) are kept but flagged: they cancel
  out in net flow and are excluded as self-loops when building the graph.

Findings from the full year (Sep 2025 - Aug 2026):

* The April and May 2026 zips both contain the ~500 trips spanning midnight
  Apr 30 -> May 1, so duplicates are also checked against the previous file.
* Timestamps are local time without a zone: trips spanning the repeated hour on
  2025-11-02 (DST fall-back) get negative durations and are dropped.
* A few stations get a new id without moving (same name, lifetimes that don't
  overlap). These are merged onto the newest id, which is the one the live GBFS
  feed uses; the mapping is written to station_aliases.csv.

    python -m citibike.clean                       # default window
    python -m citibike.clean --start 2026-08-01 --end 2026-09-01
"""

import argparse
import shutil
from pathlib import Path

import duckdb

from citibike.paths import PARQUET, PROCESSED

MIN_DURATION_S = 60
MAX_DURATION_S = 3 * 3600  # longer trips are mostly bikes not docked properly
MAX_SPEED_KMH = 35  # e-bikes are capped at 20 mph (32 km/h)
# Operator facilities that use normal-looking numeric ids (e.g. '1234.56' = 'Morgan HCT Charging')
OPERATOR_NAMES = "(?i)charging|depot|warehouse|loading dock|mechanic"


def canonical_id(col: str) -> str:
    """SQL: '5980.1' / '5980.10' / '5980.10_' -> '5980.10'; anything else unchanged."""
    return (
        f"CASE WHEN regexp_matches({col}, '^\\d+\\.\\d+_?$') "
        f"THEN printf('%.2f', CAST(rtrim({col}, '_') AS DECIMAL(9, 2))) ELSE {col} END"
    )


def haversine_m(lat1: str, lng1: str, lat2: str, lng2: str) -> str:
    return (
        f"2 * 6371000 * asin(sqrt(power(sin(radians({lat2} - {lat1}) / 2), 2) + "
        f"cos(radians({lat1})) * cos(radians({lat2})) * power(sin(radians({lng2} - {lng1}) / 2), 2)))"
    )


def build_aliases(con: duckdb.DuckDBPyConnection) -> None:
    """Temp table `alias(old_id, new_id)`: same-name stations with successive lifetimes < 200 m apart."""
    life = con.execute(
        f"""
        WITH ends AS (
            SELECT {canonical_id("start_station_id")} AS id, trim(start_station_name) AS name,
                   start_lat AS lat, start_lng AS lng, started_at AS t
            FROM read_parquet('{PARQUET}/trips/*/*.parquet', hive_partitioning = false)
            UNION ALL
            SELECT {canonical_id("end_station_id")}, trim(end_station_name), end_lat, end_lng, ended_at
            FROM read_parquet('{PARQUET}/trips/*/*.parquet', hive_partitioning = false)
        )
        SELECT id, mode(name) AS name, mode(lat) AS lat, mode(lng) AS lng, min(t)::DATE AS first, max(t)::DATE AS last
        FROM ends WHERE regexp_matches(id, '^\\d+\\.\\d{{2}}$') GROUP BY id
        """
    ).df()
    pairs = []
    for _, g in life[life.name.duplicated(keep=False)].groupby("name"):
        g = g.sort_values("first")
        newest = g.iloc[-1]
        successive = (g["first"].iloc[1:].values >= g["last"].iloc[:-1].values).all()
        dist = 111320 * ((g.lat - newest.lat) ** 2 + ((g.lng - newest.lng) * 0.758) ** 2) ** 0.5
        if successive and dist.max() < 200:
            pairs += [(old, newest.id, g.name.iloc[0]) for old in g.id.iloc[:-1]]
    con.execute("CREATE OR REPLACE TEMP TABLE alias (old_id VARCHAR, new_id VARCHAR, name VARCHAR)")
    if pairs:
        con.executemany("INSERT INTO alias VALUES (?, ?, ?)", pairs)
    con.sql("SELECT * FROM alias ORDER BY name").write_csv(str(PROCESSED / "station_aliases.csv"))
    print(f"  merged {len(pairs)} renumbered station ids")


def clean_month(src: Path, start: str, end: str, con: duckdb.DuckDBPyConnection) -> None:
    """Label one source file, append its kept trips, and accumulate report/station aggregates."""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE labelled AS
        WITH canon AS (
            SELECT *,
                   {canonical_id("start_station_id")} AS start_canon,
                   {canonical_id("end_station_id")} AS end_canon
            FROM read_parquet('{src}', hive_partitioning = false)
        ),
        raw AS (
            SELECT canon.* EXCLUDE (start_canon, end_canon),
                   coalesce(a1.new_id, start_canon) AS start_id,
                   coalesce(a2.new_id, end_canon) AS end_id,
                   epoch(ended_at - started_at) AS duration_s,
                   {haversine_m("start_lat", "start_lng", "end_lat", "end_lng")} AS distance_m,
                   row_number() OVER (PARTITION BY ride_id ORDER BY started_at) AS dup_rank
            FROM canon
            LEFT JOIN alias a1 ON a1.old_id = start_canon
            LEFT JOIN alias a2 ON a2.old_id = end_canon
        )
        SELECT *,
            CASE
                WHEN dup_rank > 1 OR ride_id IN (SELECT ride_id FROM prev_ids) THEN 'duplicate ride_id'
                WHEN started_at < TIMESTAMP '{start}' OR started_at >= TIMESTAMP '{end}' THEN 'outside study window'
                WHEN start_id IS NULL OR end_id IS NULL THEN 'missing station'
                WHEN NOT regexp_matches(start_id, '^\\d+\\.\\d{{2}}$')
                  OR NOT regexp_matches(end_id, '^\\d+\\.\\d{{2}}$')
                  OR regexp_matches(start_station_name, '{OPERATOR_NAMES}')
                  OR regexp_matches(end_station_name, '{OPERATOR_NAMES}') THEN 'non-NYC or operator station'
                WHEN duration_s < 0 THEN 'negative duration (DST)'
                WHEN duration_s < {MIN_DURATION_S} THEN 'too short'
                WHEN duration_s > {MAX_DURATION_S} THEN 'too long'
                WHEN distance_m / duration_s * 3.6 > {MAX_SPEED_KMH} THEN 'implausible speed'
            END AS drop_reason
        FROM raw
        """
    )
    source = src.parent.name.removeprefix("month=")
    con.execute(
        "INSERT INTO report SELECT ?, coalesce(drop_reason, 'kept'), count(*) FROM labelled GROUP BY ALL", [source]
    )
    # Per station/name/coordinate combination, so the final station table can take exact modes.
    con.execute(
        """
        INSERT INTO station_ends
        WITH ends AS (
            SELECT start_id AS station_id, trim(start_station_name) AS name, start_lat AS lat, start_lng AS lng,
                   1 AS departures, 0 AS arrivals, started_at AS t FROM labelled WHERE drop_reason IS NULL
            UNION ALL
            SELECT end_id, trim(end_station_name), end_lat, end_lng, 0, 1, ended_at FROM labelled WHERE drop_reason IS NULL
        )
        SELECT station_id, name, lat, lng, sum(departures), sum(arrivals), min(t), max(t) FROM ends GROUP BY ALL
        """
    )
    # Slim trip table partitioned by *start* month; names/coordinates live in the station table.
    # A source file also holds trips that started the previous month, hence the per-source filename.
    con.execute(
        f"""
        COPY (
            SELECT ride_id, started_at, ended_at, start_id, end_id,
                   rideable_type = 'electric_bike' AS is_electric, member_casual = 'member' AS is_member,
                   duration_s::FLOAT AS duration_s, distance_m::FLOAT AS distance_m,
                   start_id = end_id AS is_round_trip, strftime(started_at, '%Y%m') AS month
            FROM labelled WHERE drop_reason IS NULL ORDER BY started_at
        ) TO '{PROCESSED}/trips' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (month), OVERWRITE_OR_IGNORE,
                                  FILENAME_PATTERN 'from{source}_{{i}}')
        """
    )
    # Source files overlap at month boundaries; remember ids for the next file.
    con.execute("CREATE OR REPLACE TEMP TABLE prev_ids AS SELECT ride_id FROM labelled WHERE drop_reason IS NULL")
    print(f"  done  {source}")


def clean(start: str, end: str, con: duckdb.DuckDBPyConnection) -> None:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(PROCESSED / "trips", ignore_errors=True)
    con.execute("CREATE TEMP TABLE report (source VARCHAR, outcome VARCHAR, trips BIGINT)")
    con.execute(
        "CREATE TEMP TABLE station_ends (station_id VARCHAR, name VARCHAR, lat DOUBLE, lng DOUBLE, "
        "departures BIGINT, arrivals BIGINT, first_t TIMESTAMP, last_t TIMESTAMP)"
    )
    con.execute("CREATE OR REPLACE TEMP TABLE prev_ids (ride_id VARCHAR)")
    build_aliases(con)
    for src in sorted(PARQUET.glob("trips/month=*/trips.parquet")):
        clean_month(src, start, end, con)

    con.sql("SELECT * FROM report ORDER BY source, trips DESC").write_csv(str(PROCESSED / "cleaning_report_by_month.csv"))
    report = con.sql(
        """
        SELECT outcome, sum(trips) AS trips, round(100 * sum(trips) / sum(sum(trips)) OVER (), 3) AS pct
        FROM report GROUP BY ALL ORDER BY trips DESC
        """
    )
    report.write_csv(str(PROCESSED / "cleaning_report.csv"))
    print(report)

    # Modal name/coordinates are weighted by the number of trip ends.
    con.execute(
        f"""
        COPY (
            WITH n AS (SELECT *, departures + arrivals AS w FROM station_ends)
            SELECT station_id,
                   arg_max(name, w) AS name, arg_max(lat, w) AS lat, arg_max(lng, w) AS lng,
                   sum(departures) AS departures, sum(arrivals) AS arrivals,
                   min(first_t)::DATE AS first_seen, max(last_t)::DATE AS last_seen,
                   count(DISTINCT name) AS n_names
            FROM (SELECT station_id, name, lat, lng, sum(w) AS w, sum(departures) AS departures,
                         sum(arrivals) AS arrivals, min(first_t) AS first_t, max(last_t) AS last_t
                  FROM n GROUP BY ALL)
            GROUP BY station_id ORDER BY station_id
        ) TO '{PROCESSED}/stations.parquet' (FORMAT parquet)
        """
    )
    n_st = con.execute(f"SELECT count(*) FROM '{PROCESSED}/stations.parquet'").fetchone()[0]
    n, uniq = con.execute(
        f"SELECT count(*), count(DISTINCT ride_id) FROM read_parquet('{PROCESSED}/trips/*/*.parquet')"
    ).fetchone()
    if n != uniq:
        print(f"WARNING: {n - uniq} ride_ids occur in more than one source file")
    print(f"wrote {PROCESSED}/trips/ ({n:,} trips) and stations.parquet ({n_st} stations)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2025-09-01", help="first day of study window (inclusive)")
    p.add_argument("--end", default="2026-09-01", help="end of study window (exclusive)")
    a = p.parse_args()
    con = duckdb.connect()
    con.execute("SET memory_limit = '10GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET temp_directory = '{PROCESSED.parent / 'duckdb_tmp'}'")
    clean(a.start, a.end, con)


if __name__ == "__main__":
    main()
