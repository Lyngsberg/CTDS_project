"""Official NYC boundaries for comparing communities with administrative areas.

Neighborhood Tabulation Areas 2020 (NYC Department of City Planning, via NYC
Open Data dataset 9nt8-h7nd): 262 NTAs nested in 71 community-district
tabulation areas (CDTAs) nested in 5 boroughs.

    python -m citibike.geo        # downloads NTAs, writes data/processed/station_areas.parquet
"""

import json
import urllib.request

import pandas as pd
import shapely
from shapely.geometry import shape

from citibike.paths import DATA, PROCESSED

NTA_URL = "https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson?$limit=1000"
NTA_FILE = DATA / "external" / "nta2020.geojson"


def load_ntas() -> pd.DataFrame:
    if not NTA_FILE.exists():
        NTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(NTA_URL, NTA_FILE)
    features = json.loads(NTA_FILE.read_text())["features"]
    return pd.DataFrame(
        {
            "nta": f["properties"]["nta2020"],
            "nta_name": f["properties"]["ntaname"],
            "cdta": f["properties"]["cdta2020"],
            "borough": f["properties"]["boroname"],
            "geometry": shape(f["geometry"]),
        }
        for f in features
    )


def assign_areas(stations: pd.DataFrame, ntas: pd.DataFrame) -> pd.DataFrame:
    """station_id -> NTA/CDTA/borough by point-in-polygon; stations on piers or
    just outside a polygon (shoreline) get the nearest NTA instead."""
    points = shapely.points(stations.lng.to_numpy(), stations.lat.to_numpy())
    tree = shapely.STRtree(ntas.geometry.to_numpy())
    pt_idx, poly_idx = tree.query(points, predicate="within")
    idx = pd.Series(poly_idx, index=pt_idx).groupby(level=0).first().reindex(range(len(points)))
    outside = idx.isna().to_numpy()
    idx[outside] = tree.nearest(points[outside])
    out = ntas.iloc[idx.astype(int).to_numpy()][["nta", "nta_name", "cdta", "borough"]].reset_index(drop=True)
    out.insert(0, "station_id", stations.station_id.to_numpy())
    out["matched_by"] = ["nearest" if o else "within" for o in outside]
    return out


def main() -> None:
    stations = pd.read_parquet(PROCESSED / "stations.parquet")
    areas = assign_areas(stations, load_ntas())
    areas.to_parquet(PROCESSED / "station_areas.parquet", index=False)
    print(areas.matched_by.value_counts().to_string())
    print(areas.borough.value_counts().to_string())
    print(f"{areas.nta.nunique()} NTAs, {areas.cdta.nunique()} CDTAs with at least one station")


if __name__ == "__main__":
    main()
