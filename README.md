# 02807 CTDS: Citi Bike functional neighbourhoods

Which parts of New York work as **self-contained bike-share areas**, and how do they
change over the day and week? We analyse all 45.7 M Citi Bike trips from
Sep 2025 to Aug 2026 by clustering the station-to-station trip graph and each
station's 24-hour net-flow profile.

DTU 02807 Computational Tools for Data Science, Autumn 2026. Report: [`main.tex`](main.tex).

## Main findings

- **About a dozen functional areas.** Infomap finds 12 areas that keep 79% of all
  trips inside, against 66% for a purely geographic split (k-means on coordinates)
  with the same number of areas. Spectral clustering also beats geography at every k.
  Louvain beats it only at low resolution: at γ = 2 it does worse, because it cuts up
  the dense Manhattan core.
- **Three areas cross borough borders.** Northern Manhattan rides with the Bronx,
  Bushwick (Brooklyn) with Ridgewood (Queens), and Roosevelt Island (Manhattan) with
  Astoria (Queens). Official community districts keep no more trips inside than
  geography does, and the 130 NTAs keep only 20% (Infomap with 131 areas: 63%).
- **Only the weekday morning rush has its own structure.** The Upper East and West
  Sides join the Manhattan core, which then reaches north to 104th St instead of 59th St.
  Nights and weekends don't differ from the full year beyond random-seed noise.
- **Station profiles form a continuum, not types.** One commuting axis explains 37% of
  the variance. Davies–Bouldin and silhouette both prefer k = 2 (sources vs sinks),
  and the silhouette is low (0.25).
- **Self-contained ≠ balanced.** The trips that do cross area borders are mostly
  one-way morning commutes into the core, so bike imbalance gathers at the area borders.

## How the project meets the course requirements

| Requirement | Where |
|---|---|
| Course topic 1: graph clustering | own **modularity** and **spectral clustering** (`graph.py`, checked against igraph and a planted-partition graph), Louvain (igraph) |
| Course topic 2: general clustering | k-means and Ward on net-flow profiles, k chosen with Davies–Bouldin and silhouette (`profiles.py`) |
| Technique not taught in the course | **Infomap / the map equation** on the directed trip graph (`graph.infomap`, explained in the report and in notebook 02) |
| Large data and significant cleaning | 45.7 M trips (8.3 GB), DuckDB pipeline; undocumented problems found and fixed: station-id float corruption, renumbered stations, overlapping monthly files, daylight-saving durations, a hidden operator depot (`clean.py`, notebook 01) |
| Honest evaluation | geographic baseline, official boundaries (NMI/ARI), seed-noise baseline for time slices, failure cases (report section 5) |

## Layout

```
src/citibike/           pipeline + analysis code (installed editable via requirements.txt)
  paths.py              data locations + raw trip schema
  download.py           S3 zips -> data/raw/          (idempotent, resumes partial files)
  convert.py            zips -> data/parquet/trips/   (typed, row counts checked)
  clean.py              -> data/processed/trips/, stations.parquet, cleaning reports, station_aliases.csv
  features.py           -> data/processed/edges.parquet (graph per time slice), profiles.parquet
  geo.py                NYC neighbourhood boundaries -> data/processed/station_areas.parquet
  graph.py              modularity, spectral clustering, Louvain, Infomap, flow retention
  profiles.py           net-flow profile matrix, k-means/Ward, k selection
  plots.py              shared figure styling and station maps
  gbfs.py               live station-status collector (phase 2; implemented, not in use)
notebooks/
  01_explore.ipynb      data quality checks that motivate the cleaning rules
  02_communities.ipynb  functional areas: methods, baselines, robustness, time slices
  03_profiles.ipynb     station types from net-flow profiles, and how they sit in the areas
figures/                report figures (written by notebooks 02 and 03)
main.tex                report (≤ 11,000 characters excluding spaces; currently ~10,970)
deploy/                 systemd user service for the collector
data/                   gitignored; everything in it is rebuilt by the commands below
```

## Setup

Python 3.12. Tested on WSL2 with 12 cores and 15 GB RAM; the full data needs about 11 GB of disk.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Reproduce everything

```bash
# 1. data: ~8.3 GB download, then about 3 min of processing
python -m citibike.download 2025-09 2026-08 --dry-run   # shows what would be fetched
python -m citibike.download 2025-09 2026-08
python -m citibike.convert
python -m citibike.clean                                # --start/--end to change the window
python -m citibike.features
python -m citibike.geo                                  # downloads NTA boundaries (4.5 MB)

# 2. analysis: notebooks in order (02 takes ~6 min, mostly Infomap sweeps)
jupyter nbconvert --to notebook --execute --inplace notebooks/0*.ipynb

# 3. report (any LaTeX distribution, or upload main.tex + figures/ to Overleaf)
pdflatex main.tex
```

For a quick test, one month is enough (about 1 GB):
`python -m citibike.download 2026-08 2026-08 && python -m citibike.convert && python -m citibike.clean --start 2026-08-01 --end 2026-09-01`,
then continue with `features`, `geo` and the notebooks as above. The numbers in the
notebook text and the report refer to the full year.

### Data sources

- Trips: Citi Bike System Data, public S3 bucket <https://s3.amazonaws.com/tripdata/>
  (monthly NYC zips; `JC-*` files are Jersey City and are not used).
- Boundaries: NYC Department of City Planning, 2020 Neighborhood Tabulation Areas,
  NYC Open Data dataset `9nt8-h7nd`.

### Outputs in `data/processed/`

| File | Contents |
|---|---|
| `trips/month=YYYYMM/` | cleaned trips (canonical station ids, duration, distance, round-trip flag) |
| `stations.parquet` | one row per station: name, coordinates, trip counts, first/last day seen |
| `cleaning_report.csv`, `cleaning_report_by_month.csv` | how many trips each cleaning rule removed |
| `station_aliases.csv` | renumbered stations merged onto their newest id |
| `edges.parquet` | directed trip counts per station pair and time slice |
| `profiles.parquet` | per station, day type and hour: arrivals, departures, net flow (normalised) |
| `station_areas.parquet` | station → NTA / community district / borough |
| `station_communities.parquet` | station → Infomap / Louvain / spectral area, plus Infomap area per time slice |
| `station_profile_types.parquet` | station → net-flow type (k-means, k = 4) |
| `community_sweep.csv`, `community_slices.csv` | the numbers behind the retention/modularity and time-slice results |

## Live data (GBFS station status, not in use)

`python -m citibike.gbfs collect` polls the public station-status feed and stores
only stations whose state changed (`data/live/status/YYYY-MM-DD/HH.csv.gz`, UTC, plus a
full snapshot per hour) and the station list (`data/live/info/`; `short_name` = our
`station_id`, so `station_communities.parquet` joins directly).
`deploy/citibike-gbfs.service` runs it as a systemd user service.

## Still open

- Names and student numbers on the report title page and in the contributions section
  (`% TODO` in `main.tex`).
- Group requirement: the course expects groups of 4–5, and groups smaller than 4 on
  DTU Learn after 23 October are merged with other groups.
