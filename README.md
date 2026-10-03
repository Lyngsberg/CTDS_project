# 02807 CTDS: Citi Bike functional neighbourhoods

```
src/citibike/           pipeline code (installed editable via requirements.txt)
  paths.py              data locations + raw trip schema
  download.py           S3 zips -> data/raw/          (idempotent, resumes partial files)
  convert.py            zips -> data/parquet/trips/   (typed, row counts checked)
  clean.py              -> data/processed/trips/, stations.parquet, cleaning_report.csv
  features.py           -> data/processed/edges.parquet, profiles.parquet
notebooks/              analysis notebooks (appendix)
data/                   gitignored; everything in it is rebuilt by the scripts
main.tex                report
```

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Rebuild the data (study window Sep 2025 – Aug 2026, ~8.3 GB download)

```bash
python -m citibike.download 2025-09 2026-08 --dry-run   # shows what would be fetched
python -m citibike.download 2025-09 2026-08
python -m citibike.convert
python -m citibike.clean                                # --start/--end to change the window
python -m citibike.features
```
