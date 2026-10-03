"""Station graph and community detection.

Implemented here (course topic: graph clustering):
  * modularity(): Newman-Girvan modularity with a resolution parameter
  * spectral_clustering(): normalised spectral clustering (Ng, Jordan & Weiss 2001)
  * flow_retention(): share of trips that start and end in the same community
Wrapped libraries:
  * louvain(): igraph's multilevel modularity optimisation
  * infomap(): the map equation on the *directed* graph (not taught in the course)
"""

import random
from dataclasses import dataclass

import igraph as ig
import numpy as np
import pandas as pd
import scipy.sparse as sp
from infomap import Infomap
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import eigsh
from sklearn.cluster import KMeans

from citibike.paths import PROCESSED

MIN_TRIPS_PER_DAY = 1.0  # stations below this (whole year) are too sparse for stable communities


@dataclass
class StationGraph:
    ids: np.ndarray  # station_id per node
    W: sp.csr_matrix  # directed trip counts, W[i, j] = trips from i to j

    @property
    def A(self) -> sp.csr_matrix:
        """Undirected weights A = W + W^T (trips in either direction)."""
        return (self.W + self.W.T).tocsr()

    def subgraph(self, mask: np.ndarray) -> "StationGraph":
        return StationGraph(self.ids[mask], self.W[mask][:, mask].tocsr())


def node_set(min_trips_per_day: float = MIN_TRIPS_PER_DAY) -> np.ndarray:
    """Stations active enough in the full year; the same node set is used for every slice."""
    st = pd.read_parquet(PROCESSED / "stations.parquet")
    days = (pd.to_datetime(st.last_seen) - pd.to_datetime(st.first_seen)).dt.days + 1
    keep = (st.departures + st.arrivals) / days >= min_trips_per_day
    return st.station_id[keep].sort_values().to_numpy()


def load_graph(slice_name: str = "all", ids: np.ndarray | None = None) -> StationGraph:
    """Directed graph of one time slice over `ids`, restricted to the largest connected component."""
    ids = node_set() if ids is None else ids
    edges = pd.read_parquet(PROCESSED / "edges.parquet", filters=[("slice", "==", slice_name)])
    index = pd.Series(np.arange(len(ids)), index=ids)
    edges = edges[edges.src.isin(index.index) & edges.dst.isin(index.index)]
    W = sp.csr_matrix(
        (edges.trips.to_numpy(float), (index[edges.src].to_numpy(), index[edges.dst].to_numpy())),
        shape=(len(ids), len(ids)),
    )
    g = StationGraph(ids, W)
    _, comp = connected_components(g.A, directed=False)
    return g.subgraph(comp == np.bincount(comp).argmax())


def _one_hot(labels: np.ndarray) -> sp.csr_matrix:
    _, inv = np.unique(labels, return_inverse=True)
    return sp.csr_matrix((np.ones(len(inv)), (np.arange(len(inv)), inv)))


def modularity(A: sp.spmatrix, labels: np.ndarray, resolution: float = 1.0) -> float:
    """Q = sum_c [ L_c / 2m - gamma * (d_c / 2m)^2 ]

    L_c: total weight of edges inside community c (both directions, i.e. sum of A_ij over i, j in c),
    d_c: total degree (strength) of c, 2m: total weight sum(A).
    """
    H = _one_hot(labels)
    two_m = A.sum()
    internal = (H.T @ A @ H).diagonal()
    degree = H.T @ np.asarray(A.sum(axis=1)).ravel()
    return float(np.sum(internal / two_m - resolution * (degree / two_m) ** 2))


def spectral_embedding(A: sp.spmatrix, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Top-k eigenpairs of the normalised adjacency D^-1/2 A D^-1/2 (= bottom of the normalised Laplacian)."""
    d = np.asarray(A.sum(axis=1)).ravel()
    d_inv_sqrt = sp.diags(1 / np.sqrt(d))
    M = d_inv_sqrt @ A @ d_inv_sqrt
    vals, vecs = eigsh(M, k=k, which="LA")
    order = np.argsort(vals)[::-1]
    return vals[order], vecs[:, order]


def spectral_clustering(A: sp.spmatrix, k: int, seed: int = 0) -> np.ndarray:
    """Ng-Jordan-Weiss: embed with the top-k eigenvectors, normalise rows to unit length, run k-means."""
    _, U = spectral_embedding(A, k)
    U = U / np.linalg.norm(U, axis=1, keepdims=True)
    return KMeans(k, n_init=20, random_state=seed).fit_predict(U)


def louvain(A: sp.spmatrix, resolution: float = 1.0, seed: int = 0) -> np.ndarray:
    upper = sp.triu(A, k=1).tocoo()
    g = ig.Graph(n=A.shape[0], edges=np.column_stack([upper.row, upper.col]).tolist())
    ig.set_random_number_generator(random.Random(seed))
    return np.array(g.community_multilevel(weights=upper.data, resolution=resolution).membership)


def infomap(W: sp.spmatrix, seed: int = 0, trials: int = 10, markov_time: float = 1.0) -> np.ndarray:
    """Two-level Infomap on the directed, weighted trip graph."""
    im = Infomap(two_level=True, directed=True, silent=True, seed=seed + 1, num_trials=trials, markov_time=markov_time)
    coo = sp.coo_matrix(W)
    im.add_links(zip(coo.row.tolist(), coo.col.tolist(), coo.data.tolist()))
    im.run()
    modules = im.get_modules()
    return np.array([modules[i] for i in range(W.shape[0])])


def flow_retention(W: sp.spmatrix, labels: np.ndarray) -> float:
    """Fraction of trips whose start and end stations are in the same community."""
    H = _one_hot(labels)
    return float((H.T @ W @ H).diagonal().sum() / W.sum())


def community_table(g: StationGraph, labels: np.ndarray) -> pd.DataFrame:
    """Per community: size, trips starting there, and self-containment = share of those trips that stay inside."""
    H = _one_hot(labels)
    C = (H.T @ g.W @ H).toarray()
    out = pd.DataFrame(
        {
            "community": np.unique(labels),
            "stations": np.bincount(np.unique(labels, return_inverse=True)[1]),
            "trips_out": C.sum(axis=1),
            "self_contained": np.diag(C) / C.sum(axis=1),
        }
    )
    return out.sort_values("trips_out", ascending=False, ignore_index=True)


def align_labels(reference: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Rename communities in `labels` to the `reference` community they overlap most with
    (one-to-one, largest overlaps first); unmatched communities get new ids above max(reference)."""
    overlap = pd.crosstab(labels, reference).stack().sort_values(ascending=False)
    mapping, used = {}, set()
    for (new, ref), n in overlap.items():
        if n > 0 and new not in mapping and ref not in used:
            mapping[new] = ref
            used.add(ref)
    extra = iter(range(int(np.max(reference)) + 1, 10**6))
    return np.array([mapping[c] if c in mapping else mapping.setdefault(c, next(extra)) for c in labels])
