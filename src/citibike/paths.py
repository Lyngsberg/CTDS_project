"""Canonical data locations. Everything under DATA is gitignored and reproducible."""

from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]  # project root (src/citibike/paths.py)
DATA = PROJECT / "data"
RAW = DATA / "raw"  # zips exactly as downloaded from S3
PARQUET = DATA / "parquet"  # typed, uncleaned trips: trips/month=YYYYMM/*.parquet
PROCESSED = DATA / "processed"  # cleaned trips, station table, graphs, profiles

S3_BUCKET = "https://s3.amazonaws.com/tripdata"

TRIP_COLUMNS = {
    "ride_id": "VARCHAR",
    "rideable_type": "VARCHAR",
    "started_at": "TIMESTAMP",  # local NYC wall-clock time, no tz in source
    "ended_at": "TIMESTAMP",
    "start_station_name": "VARCHAR",
    "start_station_id": "VARCHAR",  # must stay a string: '7293.10' != '7293.1'
    "end_station_name": "VARCHAR",
    "end_station_id": "VARCHAR",
    "start_lat": "DOUBLE",
    "start_lng": "DOUBLE",
    "end_lat": "DOUBLE",
    "end_lng": "DOUBLE",
    "member_casual": "VARCHAR",
}
