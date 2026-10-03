"""Convert downloaded trip zips to typed Parquet (no cleaning, only typing).

One zip -> data/parquet/trips/month=YYYYMM/trips.parquet. Every CSV header is
checked against TRIP_COLUMNS, and the Parquet row count is checked against the
CSV line count, so schema drift or parse problems fail loudly.

    python -m citibike.convert            # all zips in data/raw
    python -m citibike.convert 202608     # selected months
"""

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

import duckdb

from citibike.paths import PARQUET, RAW, TRIP_COLUMNS


def extract_csvs(zip_path: Path, out_dir: Path) -> list[Path]:
    """Extract all trip CSVs, recursing into nested zips (the old yearly archives nest monthly zips)."""
    csvs = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename)
            if info.is_dir() or "__MACOSX" in name.parts or name.name.startswith("."):
                continue
            if name.suffix.lower() == ".csv":
                target = out_dir / name.name
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 16 << 20)
                csvs.append(target)
            elif name.suffix.lower() == ".zip":
                nested = out_dir / name.name
                with z.open(info) as src, open(nested, "wb") as dst:
                    shutil.copyfileobj(src, dst, 16 << 20)
                csvs += extract_csvs(nested, out_dir)
                nested.unlink()
    return sorted(csvs)


def count_data_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        while buf := f.read(64 << 20):
            n += buf.count(b"\n")
        f.seek(-1, 2)
        n += f.read(1) != b"\n"  # last line without trailing newline
    return n - 1  # header


def convert(zip_path: Path, out_root: Path, con: duckdb.DuckDBPyConnection, force: bool = False) -> None:
    month = zip_path.name[:6]
    out = out_root / "trips" / f"month={month}" / "trips.parquet"
    if out.exists() and not force:
        print(f"  skip  {month} (exists)")
        return
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=out_root) as tmp:
        csvs = extract_csvs(zip_path, Path(tmp))
        expected = ",".join(TRIP_COLUMNS)
        for c in csvs:
            with open(c, encoding="utf-8-sig") as f:
                header = f.readline().strip().replace('"', "")
            if header != expected:
                raise ValueError(f"{c.name}: unexpected header\n  got      {header}\n  expected {expected}")
        n_csv = sum(count_data_lines(c) for c in csvs)

        files = [str(c) for c in csvs]
        tmp_out = out.with_suffix(".parquet.tmp")
        con.execute(
            f"""
            COPY (
                SELECT * FROM read_csv(?, header = true, columns = {TRIP_COLUMNS!r},
                                       timestampformat = '%Y-%m-%d %H:%M:%S.%g')
                ORDER BY started_at
            ) TO '{tmp_out}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 500000)
            """,
            [files],
        )
        n_pq = con.execute(f"SELECT count(*) FROM read_parquet('{tmp_out}')").fetchone()[0]
        if n_pq != n_csv:
            tmp_out.unlink()
            raise ValueError(f"{month}: {n_csv} CSV lines but {n_pq} Parquet rows")
        tmp_out.rename(out)
    print(f"  done  {month}: {len(csvs)} CSVs, {n_pq:,} rows, {out.stat().st_size / 1e6:.0f} MB")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("months", nargs="*", help="YYYYMM; default: every zip in data/raw")
    p.add_argument("--force", action="store_true", help="overwrite existing Parquet")
    a = p.parse_args()

    zips = sorted(RAW.glob("[0-9]" * 6 + "-citibike-tripdata.zip"))
    if a.months:
        zips = [z for z in zips if z.name[:6] in a.months]
    PARQUET.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB'")
    for z in zips:
        convert(z, PARQUET, con, a.force)


if __name__ == "__main__":
    main()
