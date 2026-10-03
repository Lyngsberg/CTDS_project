"""Net-flow profile clustering (course topic: general clustering).

Each station is described by 48 numbers: its average weekday and weekend
net flow (arrivals - departures) per hour, divided by the station's total daily
activity. The division removes station size, so the clustering compares the
*shape and relative strength* of the daily imbalance, not how busy a station is.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering, KMeans
from sklearn.metrics import davies_bouldin_score, silhouette_score

from citibike.paths import PROCESSED


def profile_matrix(ids: np.ndarray) -> pd.DataFrame:
    """Rows: stations in `ids`; columns wd00..wd23, we00..we23 (net_norm)."""
    p = pd.read_parquet(PROCESSED / "profiles.parquet")
    p = p[p.station_id.isin(ids)]
    parts = [
        p[p.day_type == day].pivot(index="station_id", columns="hour", values="net_norm").add_prefix(prefix)
        for day, prefix in [("weekday", "wd"), ("weekend", "we")]
    ]
    X = pd.concat(parts, axis=1).fillna(0.0)
    X.columns = [f"{c[:2]}{int(c[2:]):02d}" for c in X.columns]
    return X.loc[ids]


def cluster(X: pd.DataFrame, k: int, method: str = "kmeans", seed: int = 0) -> np.ndarray:
    if method == "kmeans":
        labels = KMeans(k, n_init=20, random_state=seed).fit_predict(X)
    elif method == "ward":
        labels = AgglomerativeClustering(k, linkage="ward").fit_predict(X)
    else:
        raise ValueError(method)
    return order_by_morning_net(X, labels)


def order_by_morning_net(X: pd.DataFrame, labels: np.ndarray) -> np.ndarray:
    """Relabel clusters 0..k-1 from strongest weekday-morning source to strongest sink (stable, readable labels)."""
    am = X[[f"wd{h:02d}" for h in range(6, 10)]].sum(axis=1).to_numpy()
    means = pd.Series(am).groupby(labels).mean().sort_values()
    return pd.Series(labels).map({c: i for i, c in enumerate(means.index)}).to_numpy()


def k_selection(X: pd.DataFrame, ks=range(2, 11)) -> pd.DataFrame:
    rows = []
    for k in ks:
        for method in ("kmeans", "ward"):
            labels = cluster(X, k, method)
            rows.append(
                {
                    "k": k,
                    "method": method,
                    "davies_bouldin": davies_bouldin_score(X, labels),
                    "silhouette": silhouette_score(X, labels),
                    "smallest_cluster": np.bincount(labels).min(),
                }
            )
    return pd.DataFrame(rows)
