"""Download monthly Citi Bike trip zips from the public S3 bucket.

Idempotent: a file whose size matches S3 is skipped, and a partial file is
resumed with an HTTP Range request. Downloads go to `<name>.part` and are only
renamed once the size matches and the zip's central directory can be read.

    python -m citibike.download 2025-09 2026-08 --dry-run
    python -m citibike.download 2025-09 2026-08
"""

import argparse
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from citibike.paths import RAW, S3_BUCKET

CHUNK = 8 << 20


def list_bucket() -> dict[str, int]:
    """Return {key: size} for every object in the bucket (handles pagination)."""
    objects, token = {}, None
    while True:
        url = f"{S3_BUCKET}/?list-type=2" + (f"&continuation-token={urllib.request.quote(token)}" if token else "")
        xml = urllib.request.urlopen(url, timeout=60).read().decode()
        for key, size in re.findall(r"<Key>(.*?)</Key>.*?<Size>(\d+)</Size>", xml, re.S):
            objects[key] = int(size)
        token = next(iter(re.findall(r"<NextContinuationToken>(.*?)</NextContinuationToken>", xml)), None)
        if not token:
            return objects


def month_range(start: str, end: str) -> list[str]:
    """'2025-09', '2026-08' -> ['202509', ..., '202608']."""
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch(key: str, size: int, dest_dir: Path) -> None:
    dest = dest_dir / key
    part = dest.with_name(dest.name + ".part")
    if dest.exists() and dest.stat().st_size == size:
        print(f"  skip  {key} (complete)")
        return
    if dest.exists():  # truncated file from an earlier, interrupted download
        dest.rename(part)
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    print(f"  get   {key} ({size / 1e6:.0f} MB" + (f", resuming at {have / 1e6:.0f} MB)" if have else ")"))

    if have < size:
        req = urllib.request.Request(f"{S3_BUCKET}/{key}", headers={"Range": f"bytes={have}-"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
            done, next_report = have, have + (100 << 20)
            while chunk := r.read(CHUNK):
                f.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    print(f"        {done / size:6.1%}", flush=True)
                    next_report += 100 << 20

    if part.stat().st_size != size:
        sys.exit(f"size mismatch for {key}: {part.stat().st_size} != {size}; rerun to resume")
    with zipfile.ZipFile(part) as z:  # raises if the central directory is broken
        z.infolist()
    part.rename(dest)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("start", help="first month, YYYY-MM")
    p.add_argument("end", help="last month, YYYY-MM (inclusive)")
    p.add_argument("--dest", type=Path, default=RAW)
    p.add_argument("--dry-run", action="store_true", help="only show what would be downloaded")
    a = p.parse_args()

    objects = list_bucket()
    wanted = []
    for ym in month_range(a.start, a.end):
        key = f"{ym}-citibike-tripdata.zip"  # NYC only; JC-* files are Jersey City
        if key not in objects:
            print(f"  missing on S3: {key} (only yearly zips exist before 2024)")
            continue
        wanted.append((key, objects[key]))

    a.dest.mkdir(parents=True, exist_ok=True)
    todo = [(k, s) for k, s in wanted if not ((a.dest / k).exists() and (a.dest / k).stat().st_size == s)]
    todo_bytes = sum(s for _, s in todo)
    print(f"{len(wanted)} files in range, {len(todo)} to fetch, {todo_bytes / 1e9:.2f} GB")
    if a.dry_run:
        for k, s in todo:
            print(f"  would get {k} ({s / 1e6:.0f} MB)")
        return
    if shutil.disk_usage(a.dest).free < todo_bytes * 1.1:
        sys.exit("not enough free disk space")
    for key, size in wanted:
        fetch(key, size, a.dest)


if __name__ == "__main__":
    main()
