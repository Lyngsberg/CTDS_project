"""Collect live station status from Citi Bike's public GBFS feed (phase 2 data).

The feed has no trips, only the current state of every station (bikes and docks
available, whether it is renting/returning), refreshed every ~60 s. Past states
cannot be downloaded later, so this has to run continuously.

Storage (data/live/, UTC):
  status/YYYY-MM-DD/HH.csv(.gz)   one row per station whose counts/flags changed since
                                  the previous snapshot (~55 per minute), plus a full snapshot as
                                  the first block of every hour file. Finished
                                  hours are gzipped.
  info/station_information_<unix>.json.gz   written whenever the station list changes
                                  (maps GBFS UUIDs to short_name = our station_id)

The state of station s at time t is its last row with snapshot_ts <= t.

    python -m citibike.gbfs collect          # runs until stopped (Ctrl-C / SIGTERM)
"""

import argparse
import csv
import gzip
import hashlib
import json
import logging
import signal
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from citibike.paths import DATA

INDEX_URL = "https://gbfs.citibikenyc.com/gbfs/gbfs.json"
LIVE = DATA / "live"
POLL_S = 20  # feed ttl is 60 s; polling faster keeps us within ~20 s of every update
INFO_EVERY_S = 3600

FIELDS = [
    "num_bikes_available",
    "num_ebikes_available",
    "num_bikes_disabled",
    "num_docks_available",
    "num_docks_disabled",
    "is_installed",
    "is_renting",
    "is_returning",
    "last_reported",
]
COLUMNS = ["snapshot_ts", "station_id", *FIELDS]

log = logging.getLogger("gbfs")


def get_json(url: str, etag: str | None = None) -> tuple[dict | None, str | None]:
    """GET with gzip and ETag; returns (None, etag) when unchanged (HTTP 304)."""
    headers = {"Accept-Encoding": "gzip", "User-Agent": "DTU-02807-student-project"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return json.loads(body), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def feed_urls() -> dict[str, str]:
    index, _ = get_json(INDEX_URL)
    return {f["name"]: f["url"] for f in index["data"]["en"]["feeds"]}


class Collector:
    def __init__(self, root: Path = LIVE):
        self.root = root
        self.urls = feed_urls()
        self.status_etag = None
        self.last_updated = None
        self.state: dict[str, tuple] = {}  # station_id -> last written field values
        self.hour_file: Path | None = None
        self.info_hash = None
        self.info_checked = 0.0
        self.rows_this_hour = 0
        self.snapshots_this_hour = 0
        self.stop = False

    def save_info(self) -> None:
        info, _ = get_json(self.urls["station_information"])
        stations = sorted(info["data"]["stations"], key=lambda s: s["station_id"])
        digest = hashlib.sha256(json.dumps(stations, sort_keys=True).encode()).hexdigest()
        if digest != self.info_hash:
            out = self.root / "info" / f"station_information_{info['last_updated']}.json.gz"
            out.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(out, "wt") as f:
                json.dump(info, f)
            self.info_hash = digest
            log.info("station_information changed: %d stations -> %s", len(stations), out.name)
        self.info_checked = time.time()

    def rotate(self, ts: int) -> None:
        """Switch to the hour file for `ts`; gzip the finished one and force a full snapshot."""
        hour = datetime.fromtimestamp(ts, UTC)
        path = self.root / "status" / hour.strftime("%Y-%m-%d") / hour.strftime("%H.csv")
        if path == self.hour_file:
            return
        if self.hour_file is not None:
            log.info("%s: %d snapshots, %d rows", self.hour_file, self.snapshots_this_hour, self.rows_this_hour)
        for old in (self.root / "status").glob("*/*.csv"):  # includes leftovers from a crash
            if old != path:
                compress(old)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(COLUMNS)
        self.hour_file, self.state = path, {}
        self.rows_this_hour = self.snapshots_this_hour = 0

    def poll(self) -> None:
        status, self.status_etag = get_json(self.urls["station_status"], self.status_etag)
        if status is None or status["last_updated"] == self.last_updated:
            return
        ts = status["last_updated"]
        self.rotate(ts)
        rows = []
        for s in status["data"]["stations"]:
            values = tuple(int(s.get(k, 0)) for k in FIELDS)
            # last_reported is a heartbeat that ticks every minute for ~60% of stations;
            # comparing without it cuts rows ~30x (it is still stored on every written row)
            if self.state.get(s["station_id"]) != values[:-1]:
                self.state[s["station_id"]] = values[:-1]
                rows.append((ts, s["station_id"], *values))
        with open(self.hour_file, "a", newline="") as f:
            csv.writer(f).writerows(rows)
        self.last_updated = ts
        self.rows_this_hour += len(rows)
        self.snapshots_this_hour += 1

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        log.info("collecting into %s", self.root)
        failures = 0
        while not self.stop:
            started = time.time()
            try:
                if started - self.info_checked > INFO_EVERY_S:
                    self.save_info()
                self.poll()
                failures = 0
            except KeyboardInterrupt:
                break
            except Exception as e:  # network hiccups etc.: log and keep going
                failures += 1
                log.warning("poll failed (%d in a row): %r", failures, e)
                if failures % 10 == 0:  # maybe the feed URLs moved
                    try:
                        self.urls = feed_urls()
                    except Exception as e2:
                        log.warning("refreshing feed index failed: %r", e2)
            try:
                time.sleep(max(1.0, POLL_S - (time.time() - started)))
            except KeyboardInterrupt:
                break
        log.info("stopped")


def compress(path: Path) -> None:
    with open(path, "rb") as src, gzip.open(path.with_suffix(".csv.gz"), "wb") as dst:
        dst.write(src.read())
    path.unlink()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["collect"])
    p.add_argument("--root", type=Path, default=LIVE)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Collector(a.root).run()


if __name__ == "__main__":
    main()
