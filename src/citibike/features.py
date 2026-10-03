"""Build analysis inputs from cleaned trips: station graphs per time slice and
24-hour net-flow profiles.

Outputs (data/processed/):
  edges.parquet    slice, src, dst, trips              directed, no self-loops
  profiles.parquet station_id, day_type, hour, departures, arrivals, net, net_norm
                   (averages per day of the station's lifetime; net = arrivals - departures,
                    net_norm = net / average daily activity of the station)

US federal holidays count as weekend days. A slice is defined by the trip's
*start* time.

    python -m citibike.features
"""

import duckdb
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

from citibike.paths import PROCESSED

# name -> SQL predicate over columns day_type and hour (both from started_at)
SLICES = {
    "all": "true",
    "weekday_am": "day_type = 'weekday' AND hour BETWEEN 6 AND 9",
    "weekday_midday": "day_type = 'weekday' AND hour BETWEEN 10 AND 15",
    "weekday_pm": "day_type = 'weekday' AND hour BETWEEN 16 AND 19",
    "weekday_night": "day_type = 'weekday' AND (hour >= 20 OR hour <= 5)",
    "weekend": "day_type = 'weekend'",
}


def register_trips(con: duckdb.DuckDBPyConnection) -> None:
    """View `trips` with day_type/hour for the start time; `trip_ends` has one row per departure or arrival."""
    days = con.execute(f"SELECT min(started_at)::DATE, max(ended_at)::DATE FROM '{PROCESSED}/trips/*/*.parquet'").fetchone()
    holidays = pd.DataFrame({"day": USFederalHolidayCalendar().holidays(*days).date})
    con.register("holidays", holidays)
    day_type = "CASE WHEN dayofweek({t}) IN (0, 6) OR {t}::DATE IN (SELECT day FROM holidays) THEN 'weekend' ELSE 'weekday' END"
    con.execute(
        f"""
        CREATE OR REPLACE VIEW trips AS
        SELECT *, {day_type.format(t="started_at")} AS day_type, hour(started_at) AS hour
        FROM read_parquet('{PROCESSED}/trips/*/*.parquet', hive_partitioning = false)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW trip_ends AS
        SELECT start_id AS station_id, started_at AS t, {day_type.format(t="started_at")} AS day_type,
               1 AS departure, 0 AS arrival FROM trips
        UNION ALL
        SELECT end_id, ended_at, {day_type.format(t="ended_at")}, 0, 1 FROM trips
        """
    )


def build_edges(con: duckdb.DuckDBPyConnection) -> None:
    selects = " UNION ALL ".join(
        f"SELECT '{name}' AS slice, start_id AS src, end_id AS dst, count(*) AS trips "
        f"FROM trips WHERE NOT is_round_trip AND ({pred}) GROUP BY ALL"
        for name, pred in SLICES.items()
    )
    con.execute(f"COPY ({selects} ORDER BY slice, src, dst) TO '{PROCESSED}/edges.parquet' (FORMAT parquet)")
    print(con.sql(f"SELECT slice, count(*) AS edges, sum(trips) AS trips FROM '{PROCESSED}/edges.parquet' GROUP BY 1 ORDER BY 1"))


def build_profiles(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        COPY (
            WITH days AS (SELECT DISTINCT t::DATE AS day, day_type FROM trip_ends),
            n_days AS (  -- days of each type within the station's lifetime, to turn counts into per-day averages
                SELECT station_id, day_type, count(*) AS days
                FROM '{PROCESSED}/stations.parquet' s JOIN days ON day BETWEEN s.first_seen AND s.last_seen
                GROUP BY ALL
            ),
            grid AS (  -- every station x day_type x hour, so hours without trips become 0 instead of missing
                SELECT station_id, day_type, hour FROM n_days, range(24) r(hour)
            ),
            counts AS (
                SELECT station_id, day_type, hour(t) AS hour, sum(departure) AS dep, sum(arrival) AS arr
                FROM trip_ends GROUP BY ALL
            ),
            per_day AS (
                SELECT g.station_id, g.day_type, g.hour,
                       coalesce(dep, 0) / days AS departures, coalesce(arr, 0) / days AS arrivals
                FROM grid g JOIN n_days USING (station_id, day_type) LEFT JOIN counts USING (station_id, day_type, hour)
            )
            SELECT *, arrivals - departures AS net,
                   (arrivals - departures)
                     / nullif(sum(arrivals + departures) OVER (PARTITION BY station_id, day_type), 0) AS net_norm
            FROM per_day ORDER BY station_id, day_type, hour
        ) TO '{PROCESSED}/profiles.parquet' (FORMAT parquet)
        """
    )
    print(con.sql(f"SELECT day_type, count(DISTINCT station_id) AS stations, sum(departures) AS trips_per_day FROM '{PROCESSED}/profiles.parquet' GROUP BY 1"))


def main() -> None:
    con = duckdb.connect()
    con.execute("SET memory_limit = '10GB'")
    register_trips(con)
    build_edges(con)
    build_profiles(con)


if __name__ == "__main__":
    main()
