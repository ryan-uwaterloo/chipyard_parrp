#!/usr/bin/env python3
"""
plot_per_test.py
----------------
Generates one output SVG per test case.  All enabled metrics for a test
are shown as split violins on a single shared axis (x = metric, y = cycles).

Metric order
------------
Default order (reqtimes first):
    reqtime_load → reqtime_store → miss_penalty → eviction_time →
    llc_residual → probe_latency → dram

Override per test with the optional "order" key in TEST_CONFIGS.

Usage
-----
    python plot_per_test.py

Edit TEST_CONFIGS below to add/remove tests or toggle/reorder metrics.
Edit DIRECTORIES to point at your data.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import scienceplots                          # noqa: F401  (registers 'science' style)
import numpy as np
from matplotlib.patches import Patch

from loaders import (
    build_paths,
    load_miss_penalty,
    load_eviction_time_ctrl,
    load_eviction_time_parrp,
    load_llc_residual,
    load_probe_latency,
    load_dram,
    load_reqtimes,
    CACHE_TYPES,
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
    "data":    "../parsed/synthetics",   # L1 / LLC / probe / DRAM CSVs
    "memreq":  "../mrt_data",            # per-core memreqtime CSVs
}

# ============================================================
# Metric catalogue
# ============================================================
# Maps a metric key to its subplot metadata and a loader factory.
# The loader factory receives (paths_dict, test_config, dirs) and
# returns (stock_array, mod_array).

METRIC_DEFS = {
    "miss_penalty": {
        "ylabel": "Miss Penalty (cycles)",
        "title":  "Miss Penalty",
        "loader": lambda p, cfg, dirs: (
            load_miss_penalty(p["l1_ctrl"],  cfg["data_start"]),
            load_miss_penalty(p["l1_parrp"], cfg["data_start"]),
        ),
    },
    "eviction_time": {
        "ylabel": "Eviction Time (cycles)",
        "title":  "Eviction Time",
        "loader": lambda p, cfg, dirs: (
            load_eviction_time_ctrl(p["rel_ctrl"], cfg["data_start"]),
            load_eviction_time_parrp(
                p["rel_parrp"],
                p["l1_parrp"],
                num_sets=cfg.get("num_sets", 64),
                data_start=cfg["data_start"],
            ),
        ),
    },
    "llc_residual": {
        "ylabel": "SourceD-to-Complete (cycles)",
        "title":  "LLC Release Residual",
        "loader": lambda p, cfg, dirs: (
            load_llc_residual(p["llc_ctrl"],  cfg["data_start"]),
            load_llc_residual(p["llc_parrp"], cfg["data_start"]),
        ),
    },
    "probe_latency": {
        "ylabel": "Probe Latency (cycles)",
        "title":  "Probe Latency",
        "loader": lambda p, cfg, dirs: (
            load_probe_latency(p["probe_ctrl"],  cfg["data_start"]),
            load_probe_latency(p["probe_parrp"], cfg["data_start"]),
        ),
    },
    "dram": {
        "ylabel": "DRAM Access Time (cycles)",
        "title":  "DRAM Access Time",
        "loader": lambda p, cfg, dirs: (
            load_dram(p["dram_ctrl"],  cfg["data_start"]),
            load_dram(p["dram_parrp"], cfg["data_start"]),
        ),
    },
    "reqtime_load": {
        "ylabel": "Request Time (cycles)",
        "title":  "LSU Load Request Time",
        # Aggregates both data and inst caches, all cores
        "loader": lambda p, cfg, dirs: _load_reqtime_agg(
            cfg["test"], dirs["memreq"], "Load", ["data", "inst"],
        ),
    },
    "reqtime_store": {
        "ylabel": "Request Time (cycles)",
        "title":  "LSU Store Request Time",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: _load_reqtime_agg(
            cfg["test"], dirs["memreq"], "Store", ["data"],
        ),
    },
}

# Default display order — reqtimes first, then cache-hierarchy metrics.
# Override per test with cfg["order"] (list of metric keys).
DEFAULT_METRIC_ORDER = [
    "reqtime_load",
    "reqtime_store",
    "miss_penalty",
    "eviction_time",
    "llc_residual",
    "probe_latency",
    "dram",
]


def _load_reqtime_agg(
    test: str,
    memreq_dir: str,
    node_type: str,
    cache_types: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate reqtime across cache_types for stock and mod variants."""
    parts_stock: list[np.ndarray] = []
    parts_mod:   list[np.ndarray] = []

    for ct in cache_types:
        stock = load_reqtimes(f"{test}-ctrl",  ct, memreq_dir)[node_type]
        mod   = load_reqtimes(f"{test}-parrp", ct, memreq_dir)[node_type]
        if len(stock):
            parts_stock.append(stock)
        if len(mod):
            parts_mod.append(mod)

    arr_stock = np.concatenate(parts_stock) if parts_stock else np.array([], dtype=np.float32)
    arr_mod   = np.concatenate(parts_mod)   if parts_mod   else np.array([], dtype=np.float32)
    return arr_stock, arr_mod


# ============================================================
# Per-test configuration
# ============================================================
# Each entry in TEST_CONFIGS defines one output figure.
#
# Required keys
# -------------
# test    : str   – base test name (used to build file paths)
# label   : str   – human-readable name for titles / filenames
# metrics : dict  – {metric_key: bool}  True = include subplot
#
# Optional keys
# -------------
# output     : str        – output filename (default: f"{test}.svg")
# num_sets   : int        – L1 set count for eviction_time parrp loader (default: 64)
# data_start : int        – warm-up cutoff in cycles; rows before this are dropped
#                           (default: 50_000 — set to 0 for raw benchmarks with no warm-up)
# order      : list[str]  – metric keys in display order; only enabled metrics are shown.
#                           Omit to use DEFAULT_METRIC_ORDER (reqtimes first).
#
# To add a new test, copy an existing block and adjust the values.
# To disable a metric globally for all tests, set its bool to False here
# and you never need to touch the loader code.

TEST_CONFIGS = [
    {
        "test":  "probe-4",
        "label": "Probe",
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  False,
            "llc_residual":   False,
            "probe_latency":  True,
            "dram":           False,
            "reqtime_load":   True,
            "reqtime_store":  True,
        },
        # "output": "probe-4.svg",   # uncomment to override default
        # "num_sets": 64,
    },
    {
        "test":  "relbuf-4",
        "label": "RelBuf",
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  False,
            "dram":           False,
            "reqtime_load":   False,
            "reqtime_store":  True,
        },
    },
    {
        "test":  "nmshrs-4",
        "label": "nMSHRs",
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  False,
            "llc_residual":   False,
            "probe_latency":  False,
            "dram":           False,
            "reqtime_load":   True,
            "reqtime_store":  False,
        },
    },
    {
        "test":  "hol-4",
        "label": "HoL",
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           False,
            "reqtime_load":   True,
            "reqtime_store":  True,
        },
    },
    # ---- Template for a new test ------------------------------------
    # {
    #     "test":       "my-test-4",
    #     "label":      "MyTest",
    #     "data_start": 0,          # 0 = no warm-up skip; omit to use default (50_000)
    #     "num_sets":   128,
    #     "metrics": {
    #         "miss_penalty":   True,
    #         "eviction_time":  True,
    #         "llc_residual":   True,
    #         "probe_latency":  False,
    #         "dram":           False,
    #         "reqtime_load":   True,
    #         "reqtime_store":  True,
    #     },
    # },
]


# ============================================================
# Legend
# ============================================================

LEGEND_ELEMENTS = [
    Patch(facecolor="white", edgecolor="red",  hatch="////////", label="Stock"),
    Patch(facecolor="white", edgecolor="blue", hatch="xxxxxxxx", label="Parrp"),
]


# ============================================================
# Per-test plot
# ============================================================

def plot_one_test(cfg: dict, dirs: dict) -> None:
    test    = cfg["test"]
    label   = cfg["label"]
    metrics = cfg["metrics"]
    output  = cfg.get("output", f"{test}.svg")

    # Resolve data_start: per-test override, or module default
    from loaders import DEFAULT_DATA_START
    data_start = cfg.get("data_start", DEFAULT_DATA_START)

    # Resolve metric order: per-test override or default (reqtimes first)
    order  = cfg.get("order", DEFAULT_METRIC_ORDER)
    active = [m for m in order if metrics.get(m, False)]
    if not active:
        print(f"  [skip] {test}: no metrics enabled")
        return

    paths = build_paths(test, dirs["data"])

    print(f"\n{'='*60}")
    print(f"  Test       : {label}  ({test})")
    print(f"  data_start : {data_start}")
    print(f"  Metrics    : {', '.join(active)}")
    print(f"{'='*60}")

    # ---- Load data for each active metric ---------------------------
    data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for metric in active:
        mdef = METRIC_DEFS[metric]
        print(f"  Loading {metric} …")
        data[metric] = mdef["loader"](
            paths, {**cfg, "test": test, "data_start": data_start}, dirs
        )

    # ---- Single figure, single axis ---------------------------------
    n   = len(active)
    x   = np.arange(1, n + 1)

    # Narrow per-violin width; height stays fixed at 2.5 in
    fig, ax = plt.subplots(figsize=(1.1 * n + 0.6, 2.5))

    for i, metric in enumerate(active):
        stock_arr, mod_arr = data[metric]
        draw_violin_pair(ax, stock_arr, mod_arr, x_pos=x[i])

    # Wrap long titles onto two lines so labels stay horizontal
    def _wrap(text, max_chars=12):
        if len(text) <= max_chars:
            return text
        mid = text.rfind(" ", 0, max_chars + 1)
        if mid == -1:
            mid = text.find(" ")   # no space before limit — use first space
        if mid == -1:
            return text            # single long word, leave as-is
        return text[:mid] + "\n" + text[mid + 1:]

    ax.set_xticks(x)
    ax.set_xticklabels(
        [_wrap(METRIC_DEFS[m]["title"]) for m in active],
        rotation=0, ha="center", fontsize=7,
    )
    ax.set_ylabel("Cycles")
    ax.set_title(label)
    ax.set_xlim(0.5, n + 0.5)
    ax.grid(axis="y", linestyle="--", linewidth=0.5)
    ax.legend(handles=LEGEND_ELEMENTS, fontsize=6)

    plt.tight_layout()
    plt.savefig(output)
    plt.close(fig)
    print(f"  Saved → {output}")


# ============================================================
# Main
# ============================================================

def main():
    assert TEST_CONFIGS, "TEST_CONFIGS is empty — nothing to plot."

    for cfg in TEST_CONFIGS:
        plot_one_test(cfg, DIRECTORIES)

    print("\nDone.")


if __name__ == "__main__":
    main()