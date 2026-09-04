#!/usr/bin/env python3
"""
plot_iso_int.py
---------------
Generates one output SVG per isolation/interference test case.

Layout
------
One shared axis per figure (x = metric, y = cycles).  At each metric
x-position there are TWO split violin pairs side by side:

    ┌─────────────────────────────┐
    │  ctrl pair   │  parrp pair  │
    │  iso | int   │  iso | int   │
    └─────────────────────────────┘

Colours / hatches
-----------------
    iso-ctrl   red   ////////   (left  half of ctrl pair)
    int-ctrl   green ........   (right half of ctrl pair)
    iso-parrp  blue  xxxxxxxx   (left  half of parrp pair)
    int-parrp  orange ++++++++ (right half of parrp pair)

A thin vertical rule separates the ctrl pair from the parrp pair at each
metric position.

Filename convention
-------------------
    inter-{iso|int}-{base}-{ctrl|parrp}.csv   (and derivative suffixes)
e.g. for base="4":  inter-iso-4-ctrl.csv, inter-int-4-parrp_releases.csv …

Configuration
-------------
Edit ISO_INT_CONFIGS and DIRECTORIES below.  All metric keys, loaders, and
ordering options are identical to plot_per_test.py.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import scienceplots                          # noqa: F401
import numpy as np
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

from loaders import (
    build_paths_iso_int,
    load_miss_penalty,
    load_eviction_time_ctrl,
    load_eviction_time_parrp,
    load_llc_residual,
    load_probe_latency,
    load_dram,
    load_reqtimes,
    DEFAULT_DATA_START,
)
from violin_helpers import draw_violin_pair

plt.style.use(["science", "ieee"])
plt.rcParams.update({
    "text.usetex":     False,
    "font.family":     "serif",
    "font.serif":      ["Liberation Serif", "DejaVu Serif", "Bitstream Vera Serif", "serif"],
    "hatch.linewidth": 0.2,
})

# ============================================================
# Directories  ← edit these
# ============================================================

DIRECTORIES = {
    "data":   "../parsed/synthetics",
    "memreq": "../mrt_data",
}

# ============================================================
# Visual constants for the quad layout
# ============================================================

# Each metric occupies a slot of width SLOT_WIDTH centred on an integer x.
# Within the slot:
#   ctrl  pair is centred at x - PAIR_OFFSET
#   parrp pair is centred at x + PAIR_OFFSET
PAIR_OFFSET  = 0.28   # distance from slot centre to each pair centre
HALF_WIDTH   = 0.22   # half-width of each individual violin half
PAIR_GAP     = 0.01   # gap between the two halves within one pair

# Colours and hatches for the four roles
STYLE = {
    #              color       hatch
    "iso_ctrl":  ("red",      "////////"),
    "int_ctrl":  ("green",    "........"),
    "iso_parrp": ("blue",     "xxxxxxxx"),
    "int_parrp": ("orange",   "++++++++" ),
}

# ============================================================
# Metric catalogue  (identical structure to plot_per_test.py)
# ============================================================

def _load_reqtime_agg(
    test_variant: str,
    memreq_dir: str,
    node_type: str,
    cache_types: list[str],
) -> np.ndarray:
    """Aggregate reqtime arrays across cache_types for one variant string."""
    parts = []
    for ct in cache_types:
        arr = load_reqtimes(test_variant, ct, memreq_dir)[node_type]
        if len(arr):
            parts.append(arr)
    return np.concatenate(parts) if parts else np.array([], dtype=np.float32)


def _reqtime_loader(node_type: str, cache_types: list[str]):
    """Return a loader lambda for reqtime metrics."""
    def _loader(paths_pair, cfg, dirs):
        base = cfg["base"]
        return {
            role: _load_reqtime_agg(
                f"inter-{condition}-{base}-{variant}",
                dirs["memreq"],
                node_type,
                cache_types,
            )
            for role, condition, variant in [
                ("iso_ctrl",  "iso", "ctrl"),
                ("int_ctrl",  "int", "ctrl"),
                ("iso_parrp", "iso", "parrp-wb"),
                ("int_parrp", "int", "parrp-wb"),
            ]
        }
    return _loader


def _standard_loader(file_key_map):
    """
    Return a loader lambda for metrics that map directly to CSV files.

    file_key_map : {role: (paths_condition_key, loader_fn, extra_kwargs)}
    """
    def _loader(paths_pair, cfg, dirs):
        ds = cfg["data_start"]
        results = {}
        for role, (cond, file_key, fn, kwargs) in file_key_map.items():
            path = paths_pair[cond][file_key]
            results[role] = fn(path, ds, **kwargs)
        return results
    return _loader


# Eviction time needs special handling (parrp uses a different function)
def _eviction_loader(paths_pair, cfg, dirs):
    ds       = cfg["data_start"]
    num_sets = cfg.get("num_sets", 64)
    return {
        "iso_ctrl":  load_eviction_time_ctrl(paths_pair["iso"]["rel_ctrl"],  ds),
        "int_ctrl":  load_eviction_time_ctrl(paths_pair["int"]["rel_ctrl"],  ds),
        "iso_parrp": load_eviction_time_parrp(
            paths_pair["iso"]["rel_parrp"], paths_pair["iso"]["l1_parrp"],
            num_sets=num_sets, data_start=ds,
        ),
        "int_parrp": load_eviction_time_parrp(
            paths_pair["int"]["rel_parrp"], paths_pair["int"]["l1_parrp"],
            num_sets=num_sets, data_start=ds,
        ),
    }


METRIC_DEFS = {
    "miss_penalty": {
        "title":  "Miss Penalty",
        "loader": _standard_loader({
            "iso_ctrl":  ("iso", "l1_ctrl",  load_miss_penalty, {}),
            "int_ctrl":  ("int", "l1_ctrl",  load_miss_penalty, {}),
            "iso_parrp": ("iso", "l1_parrp", load_miss_penalty, {}),
            "int_parrp": ("int", "l1_parrp", load_miss_penalty, {}),
        }),
    },
    "eviction_time": {
        "title":  "Eviction Time",
        "loader": _eviction_loader,
    },
    "llc_residual": {
        "title":  "LLC Release\nResidual",
        "loader": _standard_loader({
            "iso_ctrl":  ("iso", "llc_ctrl",  load_llc_residual, {}),
            "int_ctrl":  ("int", "llc_ctrl",  load_llc_residual, {}),
            "iso_parrp": ("iso", "llc_parrp", load_llc_residual, {}),
            "int_parrp": ("int", "llc_parrp", load_llc_residual, {}),
        }),
    },
    "probe_latency": {
        "title":  "Probe Latency",
        "loader": _standard_loader({
            "iso_ctrl":  ("iso", "probe_ctrl",  load_probe_latency, {}),
            "int_ctrl":  ("int", "probe_ctrl",  load_probe_latency, {}),
            "iso_parrp": ("iso", "probe_parrp", load_probe_latency, {}),
            "int_parrp": ("int", "probe_parrp", load_probe_latency, {}),
        }),
    },
    "dram": {
        "title":  "DRAM Access\nTime",
        "loader": _standard_loader({
            "iso_ctrl":  ("iso", "dram_ctrl",  load_dram, {}),
            "int_ctrl":  ("int", "dram_ctrl",  load_dram, {}),
            "iso_parrp": ("iso", "dram_parrp", load_dram, {}),
            "int_parrp": ("int", "dram_parrp", load_dram, {}),
        }),
    },
    "reqtime_load": {
        "title":  "Load Request\nTime",
        "loader": _reqtime_loader("Load", ["data", "inst"]),
    },
    "reqtime_store": {
        "title":  "Store Request\nTime",
        "loader": _reqtime_loader("Store", ["data"]),
    },
}

DEFAULT_METRIC_ORDER = [
    "reqtime_load",
    "reqtime_store",
    "miss_penalty",
    "eviction_time",
    "llc_residual",
    "probe_latency",
    "dram",
]

# ============================================================
# Per-test configuration
# ============================================================
# Required keys
# -------------
# base    : str  – suffix in filename, e.g. "4" to inter-iso-4-ctrl.csv
# label   : str  – human-readable title for the figure
# metrics : dict – {metric_key: bool}
#
# Optional keys
# -------------
# output     : str       – output filename (default: f"iso-int-{base}.svg")
# num_sets   : int       – L1 set count for eviction_time parrp (default: 64)
# data_start : int       – warm-up cutoff cycles (default: 50_000; use 0 for no skip)
# order      : list[str] – metric display order (default: DEFAULT_METRIC_ORDER)

ISO_INT_CONFIGS = [
    {
        "base":  "4",
        "label": "Isolation vs Interference",
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  False,
            "llc_residual":   False,
            "probe_latency":  False,
            "dram":           False,
            "reqtime_load":   True,
            "reqtime_store":  True,
        },
    },
    # ---- Template ---------------------------------------------------
    # {
    #     "base":       "4",
    #     "label":      "My Iso/Int Test",
    #     "data_start": 0,
    #     "num_sets":   128,
    #     "output":     "my-iso-int.svg",
    #     "order":      ["miss_penalty", "eviction_time", "reqtime_load"],
    #     "metrics": {
    #         "miss_penalty":   True,
    #         "eviction_time":  True,
    #         "llc_residual":   False,
    #         "probe_latency":  False,
    #         "dram":           False,
    #         "reqtime_load":   True,
    #         "reqtime_store":  False,
    #     },
    # },
]

# ============================================================
# Legend
# ============================================================

LEGEND_ELEMENTS = [
    Patch(facecolor="white", edgecolor=STYLE["iso_ctrl"][0],
          hatch=STYLE["iso_ctrl"][1],  label="Isolation / Stock"),
    Patch(facecolor="white", edgecolor=STYLE["int_ctrl"][0],
          hatch=STYLE["int_ctrl"][1],  label="Interference / Stock"),
    Patch(facecolor="white", edgecolor=STYLE["iso_parrp"][0],
          hatch=STYLE["iso_parrp"][1], label="Isolation / Parrp"),
    Patch(facecolor="white", edgecolor=STYLE["int_parrp"][0],
          hatch=STYLE["int_parrp"][1], label="Interference / Parrp"),
]

# ============================================================
# Quad violin drawing
# ============================================================

def draw_violin_quad(ax, arrays: dict[str, np.ndarray], x_pos: int) -> None:
    """
    Draw two split violin pairs at x_pos.

    arrays : {"iso_ctrl": arr, "int_ctrl": arr, "iso_parrp": arr, "int_parrp": arr}

    Pair layout:
        ctrl  pair centred at x_pos - PAIR_OFFSET
        parrp pair centred at x_pos + PAIR_OFFSET
    """
    ctrl_x  = x_pos - PAIR_OFFSET
    parrp_x = x_pos + PAIR_OFFSET

    draw_violin_pair(
        ax,
        arrays["iso_ctrl"],
        arrays["int_ctrl"],
        x_pos=ctrl_x,
        half_width=HALF_WIDTH,
        gap=PAIR_GAP,
        stock_color=STYLE["iso_ctrl"][0],
        mod_color=STYLE["int_ctrl"][0],
        stock_hatch=STYLE["iso_ctrl"][1],
        mod_hatch=STYLE["int_ctrl"][1],
    )

    draw_violin_pair(
        ax,
        arrays["iso_parrp"],
        arrays["int_parrp"],
        x_pos=parrp_x,
        half_width=HALF_WIDTH,
        gap=PAIR_GAP,
        stock_color=STYLE["iso_parrp"][0],
        mod_color=STYLE["int_parrp"][0],
        stock_hatch=STYLE["iso_parrp"][1],
        mod_hatch=STYLE["int_parrp"][1],
    )

    # Thin rule between the two pairs
    ax.axvline(x_pos, color="gray", linewidth=0.4, linestyle=":", zorder=0)


# ============================================================
# Per-config plot
# ============================================================

def plot_one_config(cfg: dict, dirs: dict) -> None:
    base       = cfg["base"]
    label      = cfg["label"]
    metrics    = cfg["metrics"]
    output     = cfg.get("output", f"iso-int-{base}.svg")
    data_start = cfg.get("data_start", DEFAULT_DATA_START)
    order      = cfg.get("order", DEFAULT_METRIC_ORDER)
    active     = [m for m in order if metrics.get(m, False)]

    if not active:
        print(f"  [skip] base={base}: no metrics enabled")
        return

    paths_pair = build_paths_iso_int(base, dirs["data"])

    print(f"\n{'='*60}")
    print(f"  Label      : {label}  (base={base})")
    print(f"  data_start : {data_start}")
    print(f"  Metrics    : {', '.join(active)}")
    print(f"{'='*60}")

    # ---- Load -------------------------------------------------------
    loader_cfg = {**cfg, "base": base, "data_start": data_start}
    data: dict[str, dict[str, np.ndarray]] = {}
    for metric in active:
        print(f"  Loading {metric} …")
        data[metric] = METRIC_DEFS[metric]["loader"](paths_pair, loader_cfg, dirs)

    # ---- Plot -------------------------------------------------------
    n  = len(active)
    x  = np.arange(1, n + 1)

    # Wider than plot_per_test because each metric slot holds two pairs
    fig, ax = plt.subplots(figsize=(2.0 * n + 0.8, 2.5))

    for i, metric in enumerate(active):
        draw_violin_quad(ax, data[metric], x_pos=x[i])

    # x-tick labels: metric title, already wrapping long ones via \n in METRIC_DEFS
    ax.set_xticks(x)
    ax.set_xticklabels(
        [METRIC_DEFS[m]["title"] for m in active],
        rotation=0, ha="center", fontsize=6,
    )

    # Secondary tick labels: "ctrl / parrp" below each metric slot.
    # Use pure axes coordinates (transform=ax.transAxes) so labels sit
    # just below the x-axis and never blow out the figure bounding box.
    for xi in x:
        # Convert data-x to axes-x fraction
        x_frac_ctrl  = (xi - PAIR_OFFSET - ax.get_xlim()[0]) / (ax.get_xlim()[1] - ax.get_xlim()[0])
        x_frac_parrp = (xi + PAIR_OFFSET - ax.get_xlim()[0]) / (ax.get_xlim()[1] - ax.get_xlim()[0])
        ax.text(x_frac_ctrl,  -0.08, "ctrl",  ha="center", va="top",
                fontsize=5, color="gray", transform=ax.transAxes)
        ax.text(x_frac_parrp, -0.08, "parrp", ha="center", va="top",
                fontsize=5, color="gray", transform=ax.transAxes)

    ax.set_ylabel("Cycles")
    ax.set_title(label)
    ax.set_xlim(0.5, n + 0.5)
    ax.grid(axis="y", linestyle="--", linewidth=0.5)
    ax.legend(handles=LEGEND_ELEMENTS, fontsize=5, ncol=2)

    plt.tight_layout()
    plt.savefig(output)
    plt.close(fig)
    print(f"  Saved to {output}")


# ============================================================
# Main
# ============================================================

def main():
    assert ISO_INT_CONFIGS, "ISO_INT_CONFIGS is empty - nothing to plot."
    for cfg in ISO_INT_CONFIGS:
        plot_one_config(cfg, DIRECTORIES)
    print("\nDone.")


if __name__ == "__main__":
    main()