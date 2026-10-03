"""Shared figure styling: one categorical palette, one diverging ramp, station maps."""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from scipy.spatial import cKDTree

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
NEUTRAL = "#8a8985"  # baselines, reference points, "other"
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
DIVERGING = LinearSegmentedColormap.from_list("blue_gray_red", ["#184f95", "#3987e5", "#f0efec", "#ec7a79", "#b02b2a"])
LAT_ASPECT = 1 / np.cos(np.radians(40.7))  # makes 1 km look the same in both directions
ADJACENT_KM = 1.2  # communities with stations this close count as touching (map colouring)

plt.rcParams.update(
    {
        "figure.dpi": 110,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.edgecolor": NEUTRAL,
        "axes.labelcolor": TEXT_MUTED,
        "xtick.color": TEXT_MUTED,
        "ytick.color": TEXT_MUTED,
        "axes.grid": True,
        "grid.color": "#e6e5e1",
        "grid.linewidth": 0.6,
        "lines.linewidth": 2,
        "legend.frameon": False,
    }
)


def diverging_colors(n: int) -> list:
    """n colours from the blue (source) pole through gray to the red (sink) pole."""
    return [DIVERGING(x) for x in np.linspace(0, 1, n)]


def map_colors(lat: np.ndarray, lng: np.ndarray, labels: np.ndarray) -> dict:
    """Greedy colouring so communities that touch on the map never share a colour.

    Adjacency = two stations of different communities within `ADJACENT_KM` of each other, which
    also catches neighbours across the rivers. Each community takes the least-used allowed colour.
    Identity never relies on colour alone: maps also print the community number.
    """
    xy = np.c_[lng / LAT_ASPECT, lat] * 111.32  # km
    lab = np.asarray(labels)
    touching = {c: set() for c in np.unique(lab)}
    for i, j in cKDTree(xy).query_pairs(ADJACENT_KM):
        if lab[i] != lab[j]:
            touching[lab[i]].add(lab[j])
            touching[lab[j]].add(lab[i])
    colors, uses = {}, dict.fromkeys(CATEGORICAL, 0)
    for c in pd.Series(lab).value_counts().index:  # biggest first
        allowed = [col for col in CATEGORICAL if col not in {colors.get(o) for o in touching[c]}]
        colors[c] = min(allowed, key=lambda col: (uses[col], CATEGORICAL.index(col))) if allowed else NEUTRAL
        uses[colors[c]] = uses.get(colors[c], 0) + 1
    return colors


def community_map(ax, stations: pd.DataFrame, labels: np.ndarray, title: str = "", names: dict | None = None,
                  s: float = 4, fontsize: float = 8):
    """Scatter stations coloured by community, with the community number (or name) at its median position."""
    labels = np.asarray(labels)
    colors = map_colors(stations.lat.to_numpy(), stations.lng.to_numpy(), labels)
    ax.scatter(stations.lng, stations.lat, c=[colors[c] for c in labels], s=s, linewidths=0)
    for c in np.unique(labels):
        m = labels == c
        ax.annotate(
            str(names.get(c, c)) if names else str(c),
            (np.median(stations.lng[m]), np.median(stations.lat[m])),
            fontsize=fontsize, weight="bold", color=TEXT, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75),
        )
    style_map(ax, title)


def style_map(ax, title: str = ""):
    ax.set_aspect(LAT_ASPECT)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, fontsize=10, color=TEXT)


def relabel_by_size(labels: np.ndarray) -> np.ndarray:
    """Community ids 1..k, largest community first, so numbers are comparable across figures."""
    s = pd.Series(np.asarray(labels))
    rank = s.value_counts().rank(method="first", ascending=False).astype(int)
    return s.map(rank).to_numpy()


def to_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(255 * v) for v in to_rgb(c))
