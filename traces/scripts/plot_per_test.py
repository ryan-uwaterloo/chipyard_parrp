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
    build_paths_variants,
    load_miss_penalty,
    load_eviction_time_ctrl,
    load_eviction_time_parrp,
    load_llc_residual,
    load_probe_latency,
    load_dram,
    load_reqtimes,
    load_dram_write,
    load_l1_occup,
    load_llc_occup_acq,
    load_llc_occup_rel,
    load_evict_queue,
    load_miss_penalty_faceted,
    load_probe_latency_faceted,
    load_l1_occup_faceted,
    load_llc_occup_faceted,
    get_cores,
    CACHE_TYPES,
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
            load_eviction_time_parrp(
                p["rel_ctrl"],
                p["l1_ctrl"],
                num_sets=cfg.get("num_sets", 64),
                data_start=cfg["data_start"],
            ),
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
        "loader": lambda p, cfg, dirs: _load_reqtime_agg(
            cfg["test"], dirs["memreq"], "Load", ["data", "inst"],
            num_cores=cfg.get("num_cores", 4),
        ),
    },
    "reqtime_store": {
        "ylabel": "Request Time (cycles)",
        "title":  "LSU Store Request Time",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: _load_reqtime_agg(
            cfg["test"], dirs["memreq"], "Store", ["data"],
            num_cores=cfg.get("num_cores", 4),
        ),
    },
    "l1_occupancy": {
        "ylabel": "L1 Occupancy Time (cycles)",
        "title":  "L1 MSHR Occupancy",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_l1_occup(p["l1_ctrl"],  cfg["data_start"]),
            load_l1_occup(p["l1_parrp"], cfg["data_start"]),
        ),
    },
    "llc_occupancy_acq": {
        "ylabel": "LLC Occupancy Time (cycles)",
        "title":  "LLC MSHR Occupancy (Acquires)",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_llc_occup_acq(p["llc_occup_ctrl"],  cfg["data_start"]),
            load_llc_occup_acq(p["llc_occup_parrp"], cfg["data_start"]),
        ),
    },
    "llc_occupancy_rel": {
        "ylabel": "LLC Occupancy Time (cycles)",
        "title":  "LLC MSHR Occupancy (Releases)",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_llc_occup_rel(p["llc_occup_ctrl"],  cfg["data_start"]),
            load_llc_occup_rel(p["llc_occup_parrp"], cfg["data_start"]),
        ),
    },
    "dram_write": {
        "ylabel": "DRAM Write Time (cycles)",
        "title":  "DRAM Write Time",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_dram_write(p["dram_write_ctrl"],  cfg["data_start"]),
            load_dram_write(p["dram_write_parrp"], cfg["data_start"]),
        ),
    },
    "relu_arb": {
        "ylabel": "Release Unit Arbitration Time",
        "title":  "Release Unit Arbitration Time",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_evict_queue(p["relu_arb_ctrl"],  cfg["data_start"]),
            load_evict_queue(p["relu_arb_parrp"], cfg["data_start"]),
        ),
    },
}

ABLATION_METRIC_DEFS = {
    "miss_penalty": {
        "ylabel": "Miss Penalty (cycles)",
        "title":  "Miss Penalty",
        "loader": lambda p, cfg, dirs: (
            load_miss_penalty(p["l1_a"], cfg["data_start"]),
            load_miss_penalty(p["l1_b"], cfg["data_start"]),
        ),
    },
    "eviction_time": {
        "ylabel": "Eviction Time (cycles)",
        "title":  "Eviction Time",
        "loader": lambda p, cfg, dirs: (
            load_eviction_time_parrp(
                p["rel_a"], p["l1_a"],
                num_sets=cfg.get("num_sets", 64),
                data_start=cfg["data_start"],
            ),
            load_eviction_time_parrp(
                p["rel_b"], p["l1_b"],
                num_sets=cfg.get("num_sets", 64),
                data_start=cfg["data_start"],
            ),
        ),
    },
    "llc_residual": {
        "ylabel": "SourceD-to-Complete (cycles)",
        "title":  "LLC Release Residual",
        "loader": lambda p, cfg, dirs: (
            load_llc_residual(p["llc_a"], cfg["data_start"]),
            load_llc_residual(p["llc_b"], cfg["data_start"]),
        ),
    },
    "probe_latency": {
        "ylabel": "Probe Latency (cycles)",
        "title":  "Probe Latency",
        "loader": lambda p, cfg, dirs: (
            load_probe_latency(p["probe_a"], cfg["data_start"]),
            load_probe_latency(p["probe_b"], cfg["data_start"]),
        ),
    },
    "dram": {
        "ylabel": "DRAM Access Time (cycles)",
        "title":  "DRAM Access Time",
        "loader": lambda p, cfg, dirs: (
            load_dram(p["dram_a"], cfg["data_start"]),
            load_dram(p["dram_b"], cfg["data_start"]),
        ),
    },
    "dram_write": {
        "ylabel": "DRAM Write Access Time (cycles)",
        "title":  "DRAM Write Access Time",
        "loader": lambda p, cfg, dirs: (
            load_dram_write(p["dram_w_a"], cfg["data_start"]),
            load_dram_write(p["dram_w_b"], cfg["data_start"]),
        ),
    },
    "reqtime_load": {
        "ylabel": "Request Time (cycles)",
        "title":  "LSU Load Request Time",
        "loader": lambda p, cfg, dirs: _load_reqtime_agg_variants(
            cfg["test"], cfg["variant_a"], cfg["variant_b"], dirs["memreq"],
            "Load", ["data", "inst"],
            num_cores=cfg.get("num_cores", 4),
            cores=cfg.get("cores"),
        ),
    },
    "reqtime_store": {
        "ylabel": "Request Time (cycles)",
        "title":  "LSU Store Request Time",
        "loader": lambda p, cfg, dirs: _load_reqtime_agg_variants(
            cfg["test"], cfg["variant_a"], cfg["variant_b"], dirs["memreq"],
            "Store", ["data"],
            num_cores=cfg.get("num_cores", 4),
            cores=cfg.get("cores"),
        ),
    },
    "l1_occupancy": {
        "ylabel": "L1 Occupancy Time (cycles)",
        "title":  "L1 MSHR Occupancy",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_l1_occup(p["l1_a"],  cfg["data_start"]),
            load_l1_occup(p["l1_b"], cfg["data_start"]),
        ),
    },
    "llc_occupancy_acq": {
        "ylabel": "LLC Occupancy Time (cycles)",
        "title":  "LLC MSHR Occupancy (Acquires)",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_llc_occup_acq(p["llc_occup_a"],  cfg["data_start"]),
            load_llc_occup_acq(p["llc_occup_b"], cfg["data_start"]),
        ),
    },
    "llc_occupancy_rel": {
        "ylabel": "LLC Occupancy Time (cycles)",
        "title":  "LLC MSHR Occupancy (Releases)",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_llc_occup_rel(p["llc_occup_a"],  cfg["data_start"]),
            load_llc_occup_rel(p["llc_occup_b"], cfg["data_start"]),
        ),
    },
    "relu_arb": {
        "ylabel": "Release Unit Arbitration Time",
        "title":  "Release Unit Arbitration Time",
        # Stores only come from data cache
        "loader": lambda p, cfg, dirs: (
            load_evict_queue(p["relu_arb_a"],  cfg["data_start"]),
            load_evict_queue(p["relu_arb_b"], cfg["data_start"]),
        ),
    },
}

# Default display order — reqtimes first, then cache-hierarchy metrics.
# Override per test with cfg["order"] (list of metric keys).
DEFAULT_METRIC_ORDER = [
    "reqtime_load", # LSU LOAD -> GrantData(SourceD)
    "reqtime_store", # LSU STORE -> ClaimMSHR
    "miss_penalty", # ClaimMSHR -> GrantData(SourceD)
    "eviction_time", # Release[Data] (SourceC) -> ReleaseAck (SinkD)
    "llc_residual", # GrantData(SourceD) -> Directory Write
    "probe_latency", # Arb_for_SourceB -> ProbeAck[Data]
    "dram", # Acquire(SourceA) -> GrantData(SinkD)
]

# ---- facet_by dispatch ------------------------------------------------
# (first-variant key, second-variant key) into the paths dict, per CSV family.
FACET_PATH_KEYS = {
    "ctrl_parrp": {
        "l1":        ("l1_ctrl", "l1_parrp"),
        "llc":       ("llc_ctrl", "llc_parrp"),
        "probe":     ("probe_ctrl", "probe_parrp"),
        "llc_occup": ("llc_occup_ctrl", "llc_occup_parrp"),
    },
    "ablation": {
        "l1":        ("l1_a", "l1_b"),
        "llc":       ("llc_a", "llc_b"),
        "probe":     ("probe_a", "probe_b"),
        "llc_occup": ("llc_occup_a", "llc_occup_b"),
    },
}


def _load_faceted(
    metric: str, groups: list[dict], paths: dict, keys: dict,
    test: str, data_start: int, join_by_core: bool,
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Shared facet_by dispatch for plot_one_test and plot_ablation_test."""
    l1a, l1b = keys["l1"]
    llca, llcb = keys["llc"]

    if metric == "miss_penalty":
        return load_miss_penalty_faceted(
            paths[l1a], paths[l1b], paths[llca], paths[llcb],
            groups, data_start=data_start, label=test, join_by_core=join_by_core,
        )
    if metric == "l1_occupancy":
        return load_l1_occup_faceted(
            paths[l1a], paths[l1b], paths[llca], paths[llcb],
            groups, data_start=data_start, label=test, join_by_core=join_by_core,
        )
    if metric == "probe_latency":
        pa, pb = keys["probe"]
        return load_probe_latency_faceted(
            paths[pa], paths[pb], paths[llca], paths[llcb],
            groups, data_start=data_start, label=test,
        )
    if metric in ("llc_occupancy_acq", "llc_occupancy_rel"):
        oa, ob = keys["llc_occup"]
        return load_llc_occup_faceted(
            paths[oa], paths[ob], groups,
            kind=metric.rsplit("_", 1)[1], data_start=data_start,
        )
    raise NotImplementedError(f"facet_by not wired up for '{metric}' yet")


def _load_reqtime_agg(
    test: str,
    memreq_dir: str,
    node_type: str,
    cache_types: list[str],
    num_cores: int = 4,
) -> tuple[np.ndarray, np.ndarray]:
    cores = get_cores(num_cores)

    parts_stock: list[np.ndarray] = []
    parts_mod:   list[np.ndarray] = []

    for ct in cache_types:
        stock = load_reqtimes(f"{test}-ctrl",  ct, memreq_dir, cores=cores)[node_type]
        mod   = load_reqtimes(f"{test}-parrp-wb", ct, memreq_dir, cores=cores)[node_type]
        if len(stock):
            parts_stock.append(stock)
        if len(mod):
            parts_mod.append(mod)

    arr_stock = np.concatenate(parts_stock) if parts_stock else np.array([], dtype=np.float32)
    arr_mod   = np.concatenate(parts_mod)   if parts_mod   else np.array([], dtype=np.float32)
    return arr_stock, arr_mod

def _load_reqtime_agg_variants(
    test: str,
    variant_a: str,
    variant_b: str,
    memreq_dir: str,
    node_type: str,
    cache_types: list[str],
    num_cores: int = 4,
    cores: list[int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    if cores is None:
        cores = get_cores(num_cores)

    parts_a: list[np.ndarray] = []
    parts_b: list[np.ndarray] = []

    for ct in cache_types:
        a = load_reqtimes(f"{test}-{variant_a}", ct, memreq_dir, cores=cores)[node_type]
        b = load_reqtimes(f"{test}-{variant_b}", ct, memreq_dir, cores=cores)[node_type]
        if len(a):
            parts_a.append(a)
        if len(b):
            parts_b.append(b)

    arr_a = np.concatenate(parts_a) if parts_a else np.array([], dtype=np.float32)
    arr_b = np.concatenate(parts_b) if parts_b else np.array([], dtype=np.float32)
    return arr_a, arr_b

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
# facet_by   : dict        – {metric_key: [group, ...]} — split a metric into multiple
#                            x-axis slots instead of one aggregate slot. Currently supported
#                            for "miss_penalty", "probe_latency", "l1_occupancy",
#                            "llc_occupancy_acq" and "llc_occupancy_rel".
#                            miss_penalty / l1_occupancy / probe_latency join against the LLC CSV's
#                            Address/state columns (see loaders.attach_state_*);
#                            llc_occupancy_* read the state flags straight from the -llc-mshr CSV.
#                            "Load"/"Store" flags are only valid for llc_occupancy_acq (not _rel).
#                            "cores" is only meaningful for 4-core tests (SOURCE_ID_MAP); omit it
#                            for 6/8-core tests, and set join_by_core=False in the test config
#                            for miss_penalty / l1_occupancy there.
#                            Each group is:
#                              {
#                                "cores": [int, ...],   – core IDs to include (OR'd together)
#                                "flags": [str, ...],   – state filters, AND'd together.
#                                                          Entries matching a FLAG_COLUMNS name
#                                                          (NeedDRAM/NeedProbe/Evicting/BackInv)
#                                                          filter on that boolean; anything else
#                                                          is matched against Opcode instead.
#                                "label": str,          – exact x-tick text for this slot
#                                                          (no auto-generation — always required)
#                              }
#                            A metric present in `metrics` with True AND a key in `facet_by`
#                            contributes one x-axis slot per group (not one slot for the whole
#                            metric). A metric with no facet_by entry falls back to the single
#                            aggregate slot from METRIC_DEFS, as before.
#                            NOTE: rows that fail to join against an LLC transaction are dropped
#                            with a [WARN] count printed — check the console output after
#                            regenerating configs that lean on faceting.
#
# To add a new test, copy an existing block and adjust the values.
# To disable a metric globally for all tests, set its bool to False here
# and you never need to touch the loader code.
TEST_CONFIGS = [
    {
        "test":  "probe-gen-4",
        "label": "Probe",
        "order": ["probe_latency", "miss_penalty", "l1_occupancy", "llc_occupancy_acq"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["Store"], "label": "MissPenalty-Stores"},
                {"flags": ["Load"], "label": "MissPenalty-Loads"},
            ],
            "l1_occupancy": [
                {"flags": ["Store"], "label": "L1MSHR-Stores"},
                {"flags": ["Load"], "label": "L1MSHR-Loads"},
            ],
            # Load/Store is only meaningful for acquires, so releases stay aggregate
            "llc_occupancy_acq": [
                {"flags": ["Store"], "label": "LLCMSHR-Stores"},
                {"flags": ["Load"], "label": "LLCMSHR-Loads"},
            ],
        },
    },
    {
        "test":  "probe-gen-6",
        "label": "Probe",
        "order": ["probe_latency", "miss_penalty", "l1_occupancy", "llc_occupancy_acq"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["Store"], "label": "MissPenalty-Stores"},
                {"flags": ["Load"], "label": "MissPenalty-Loads"},
            ],
            "l1_occupancy": [
                {"flags": ["Store"], "label": "L1MSHR-Stores"},
                {"flags": ["Load"], "label": "L1MSHR-Loads"},
            ],
            # Load/Store is only meaningful for acquires, so releases stay aggregate
            "llc_occupancy_acq": [
                {"flags": ["Store"], "label": "LLCMSHR-Stores"},
                {"flags": ["Load"], "label": "LLCMSHR-Loads"},
            ],
        },
    },
    {
        "test":  "probe-gen-8",
        "label": "Probe",
        "order": ["probe_latency", "miss_penalty", "l1_occupancy", "llc_occupancy_acq"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["Store"], "label": "MissPenalty-Stores"},
                {"flags": ["Load"], "label": "MissPenalty-Loads"},
            ],
            "l1_occupancy": [
                {"flags": ["Store"], "label": "L1MSHR-Stores"},
                {"flags": ["Load"], "label": "L1MSHR-Loads"},
            ],
            # Load/Store is only meaningful for acquires, so releases stay aggregate
            "llc_occupancy_acq": [
                {"flags": ["Store"], "label": "LLCMSHR-Stores"},
                {"flags": ["Load"], "label": "LLCMSHR-Loads"},
            ],
        },
    },
    {
        "test":  "hol-gen-4",
        "label": "HoL",
        "order": ["eviction_time", "probe_latency", "miss_penalty", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["NeedProbe"], "label": "MissPenalty-Probes"},
                {"flags": ["!NeedProbe"], "label": "MissPenalty-Rels"},
            ],
            "l1_occupancy": [
                {"flags": ["NeedProbe"], "label": "L1MSHR-Probes"},
                {"flags": ["!NeedProbe"], "label": "L1MSHR-Rels"},
            ],
            "llc_occupancy_acq": [
                {"flags": ["NeedProbe"], "label": "LLCAcq-Probes"},
                {"flags": ["!NeedProbe"], "label": "LLCAcq-NoProbe"},
            ],
        },
    },
    {
        "test":  "hol-gen-6",
        "label": "HoL",
        "order": ["eviction_time", "probe_latency", "miss_penalty", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["NeedProbe"], "label": "MissPenalty-Probes"},
                {"flags": ["!NeedProbe"], "label": "MissPenalty-Rels"},
            ],
            "l1_occupancy": [
                {"flags": ["NeedProbe"], "label": "L1MSHR-Probes"},
                {"flags": ["!NeedProbe"], "label": "L1MSHR-Rels"},
            ],
            "llc_occupancy_acq": [
                {"flags": ["NeedProbe"], "label": "LLCAcq-Probes"},
                {"flags": ["!NeedProbe"], "label": "LLCAcq-NoProbe"},
            ],
        },
    },
    {
        "test":  "hol-gen-8",
        "label": "HoL",
        "order": ["eviction_time", "probe_latency", "miss_penalty", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "join_by_core": False,
        "facet_by": {
            "miss_penalty": [
                {"flags": ["NeedProbe"], "label": "MissPenalty-Probes"},
                {"flags": ["!NeedProbe"], "label": "MissPenalty-Rels"},
            ],
            "l1_occupancy": [
                {"flags": ["NeedProbe"], "label": "L1MSHR-Probes"},
                {"flags": ["!NeedProbe"], "label": "L1MSHR-Rels"},
            ],
            "llc_occupancy_acq": [
                {"flags": ["NeedProbe"], "label": "LLCAcq-Probes"},
                {"flags": ["!NeedProbe"], "label": "LLCAcq-NoProbe"},
            ],
        },
    },
    {
        "test":  "relbuf-gen-8",
        "label": "Release 8",
        "order": ["eviction_time", "miss_penalty", "reqtime_store", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
    },
    {
        "test":  "relbuf-gen-6",
        "label": "Release 6",
        "order": ["eviction_time", "miss_penalty", "reqtime_store", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
    },
    {
        "test":  "relbuf-gen-4",
        "label": "Release 4",
        "order": ["eviction_time", "miss_penalty", "reqtime_store", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
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
    #     # Optional — split miss_penalty/probe_latency into per-core/per-state
    #     # slots instead of one aggregate slot. Omit entirely for the default
    #     # aggregate behavior.
    #     "facet_by": {
    #         "miss_penalty": [
    #             {"cores": [0, 1], "flags": ["NeedDRAM", "BackInv"], "label": "C0-1\nDRAM+BackInv"},
    #             {"cores": [2, 3], "flags": ["NeedDRAM"],            "label": "C2-3\nDRAM"},
    #         ],
    #     },
    # },
]

ABLATION_CONFIGS = [
    {
        "test":   "mempressure-gen-4",
        "label":  "DRAM Arbitration Ablation",
        "output": "mempressure-ablation-gen.svg",
        "variant_a": "parrp-wb-splitprio",
        "variant_b": "parrp-wb",
        "data_start": 200_000,
        "order": ["reqtime_load", "reqtime_store", "miss_penalty", "dram", "dram_write", "relu_arb", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
        "facet_by": {
            "miss_penalty": [
                {"cores": [0], "flags": [], "label": "MissPenalty-C0"},
                {"cores": [1, 2, 3], "flags": [], "label": "MissPenalty-Traffic"},
            ],
            "l1_occupancy": [
                {"cores": [0], "flags": [], "label": "L1MSHR-C0"},
                {"cores": [1, 2, 3], "flags": [], "label": "L1MSHR-Traffic"},
            ],
        },
    },
    {
        "test":   "mshrs-gen-4",
        "label":  "nMSHRs Ablation",
        "output": "nmshrs-ablation-gen.svg",
        "variant_a": "ctrl-stock-mshrs",
        "variant_b": "ctrl-20-mshrs",
        "cores": [0], # only affects LSU metrics
        "data_start": 200_000,
        "order": ["miss_penalty", "reqtime_load", "dram", "l1_occupancy", "llc_occupancy_acq", "llc_occupancy_rel"],
        "metrics": {
            "miss_penalty":   True,
            "eviction_time":  True,
            "llc_residual":   True,
            "probe_latency":  True,
            "dram":           True,
            "reqtime_load":   True,
            "reqtime_store":  True,
            "l1_occupancy":   True,
            "llc_occupancy_acq": True,
            "llc_occupancy_rel": True,
            "dram_write":     True,
            "relu_arb":       True,
        },
    },
]

# ============================================================
# Ablation configuration
# ============================================================
# Each entry compares two named variants of the SAME base test against
# each other (e.g. two MSHR-count configs), rather than ctrl vs parrp.
#
# Required keys
# -------------
# test        : str  – base test name (used to build file paths)
# label       : str  – human-readable name for titles / filenames
# variant_a   : str  – first variant suffix (e.g. "ctrl-stock-mshrs")
# variant_b   : str  – second variant suffix (e.g. "ctrl-20-mshrs")
# metrics     : dict – {metric_key: bool}  True = include subplot
#
# Optional keys
# -------------
# output        : str        – output filename (default: f"{test}-ablation.svg")
# legend_labels : (str, str) – legend text for (variant_a, variant_b)
#                              (default: the variant names themselves)
# data_start    : int         – warm-up cutoff in cycles (default: 50_000)
# order         : list[str]   – metric keys in display order (default: DEFAULT_METRIC_ORDER)
# cores         : list[int]   – restricts which cores' LSU reqtime data is aggregated
#                               (only affects reqtime_load/reqtime_store; miss_penalty/
#                               probe_latency facet_by groups set their own cores instead)
# facet_by      : dict        – same shape and semantics as in TEST_CONFIGS (see above);
#                               "miss_penalty" and "probe_latency" only. Groups are matched
#                               against variant_a's and variant_b's joined LLC state the same
#                               way stock/mod are joined in the non-ablation case.


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
    facet_by = cfg.get("facet_by", {})

    data_start = cfg.get("data_start", DEFAULT_DATA_START)

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

    # ---- Build ordered slot list: (title, stock_arr, mod_arr) ----------
    slots: list[tuple[str, np.ndarray, np.ndarray]] = []

    for metric in active:
        if metric in facet_by:
            groups = facet_by[metric]
            print(f"  Loading {metric} (faceted, {len(groups)} groups) …")
            faceted = _load_faceted(
                metric, groups, paths, FACET_PATH_KEYS["ctrl_parrp"], test,
                data_start, cfg.get("join_by_core", True),
            )
            slots.extend(faceted)
        else:
            mdef = METRIC_DEFS[metric]
            print(f"  Loading {metric} …")
            stock_arr, mod_arr = mdef["loader"](
                paths, {**cfg, "test": test, "data_start": data_start}, dirs
            )
            slots.append((mdef["title"], stock_arr, mod_arr))

    if not slots:
        print(f"  [skip] {test}: no data slots produced")
        return

    n = len(slots)
    x = np.arange(1, n + 1)
    fig, ax = plt.subplots(figsize=(1.1 * n + 0.6, 2.5))

    for i, (_, stock_arr, mod_arr) in enumerate(slots):
        draw_violin_pair(ax, stock_arr, mod_arr, x_pos=x[i])

    def _wrap(text, max_chars=12):
        if len(text) <= max_chars:
            return text
        mid = text.rfind(" ", 0, max_chars + 1)
        if mid == -1:
            mid = text.find(" ")
        if mid == -1:
            return text
        return text[:mid] + "\n" + text[mid + 1:]

    ax.set_xticks(x)
    ax.set_xticklabels([_wrap(t) for t, _, _ in slots], rotation=0, ha="center", fontsize=7)
    ax.set_ylabel("Cycles")
    ax.set_title(label)
    ax.set_xlim(0.5, n + 0.5)
    ax.grid(axis="y", linestyle="--", linewidth=0.5)
    ax.legend(handles=LEGEND_ELEMENTS, fontsize=6)

    plt.tight_layout()
    plt.savefig(output)
    plt.close(fig)
    print(f"  Saved → {output}")

def plot_ablation_test(cfg: dict, dirs: dict) -> None:
    test          = cfg["test"]
    variant_a     = cfg["variant_a"]
    variant_b     = cfg["variant_b"]
    label         = cfg["label"]
    metrics       = cfg["metrics"]
    output        = cfg.get("output", f"{test}-ablation.svg")
    legend_labels = cfg.get("legend_labels", (variant_a, variant_b))
    facet_by      = cfg.get("facet_by", {})

    data_start = cfg.get("data_start", DEFAULT_DATA_START)

    order  = cfg.get("order", DEFAULT_METRIC_ORDER)
    active = [m for m in order if metrics.get(m, False)]
    if not active:
        print(f"  [skip] {test}: no metrics enabled")
        return

    paths = build_paths_variants(test, variant_a, variant_b, dirs["data"])

    print(f"\n{'='*60}")
    print(f"  Ablation   : {label}  ({variant_a} vs {variant_b})")
    print(f"  data_start : {data_start}")
    print(f"  Metrics    : {', '.join(active)}")
    print(f"{'='*60}")

    # ---- Build ordered slot list: (title, arr_a, arr_b) ----------------
    slots: list[tuple[str, np.ndarray, np.ndarray]] = []

    for metric in active:
        if metric in facet_by:
            groups = facet_by[metric]
            print(f"  Loading {metric} (faceted, {len(groups)} groups) …")
            faceted = _load_faceted(
                metric, groups, paths, FACET_PATH_KEYS["ablation"], test,
                data_start, cfg.get("join_by_core", True),
            )
            slots.extend(faceted)
        else:
            mdef = ABLATION_METRIC_DEFS[metric]
            print(f"  Loading {metric} …")
            arr_a, arr_b = mdef["loader"](
                paths, {**cfg, "test": test, "data_start": data_start}, dirs
            )
            slots.append((mdef["title"], arr_a, arr_b))

    if not slots:
        print(f"  [skip] {test}: no data slots produced")
        return

    n = len(slots)
    x = np.arange(1, n + 1)
    fig, ax = plt.subplots(figsize=(1.1 * n + 0.6, 2.5))

    for i, (_, arr_a, arr_b) in enumerate(slots):
        draw_violin_pair(ax, arr_a, arr_b, x_pos=x[i])

    def _wrap(text, max_chars=12):
        if len(text) <= max_chars:
            return text
        mid = text.rfind(" ", 0, max_chars + 1)
        if mid == -1:
            mid = text.find(" ")
        if mid == -1:
            return text
        return text[:mid] + "\n" + text[mid + 1:]

    ax.set_xticks(x)
    ax.set_xticklabels([_wrap(t) for t, _, _ in slots], rotation=0, ha="center", fontsize=7)
    ax.set_ylabel("Cycles")
    ax.set_title(label)
    ax.set_xlim(0.5, n + 0.5)
    ax.grid(axis="y", linestyle="--", linewidth=0.5)

    legend_elements = [
        Patch(facecolor="white", edgecolor="red",  hatch="////////", label=legend_labels[0]),
        Patch(facecolor="white", edgecolor="blue", hatch="xxxxxxxx", label=legend_labels[1]),
    ]
    ax.legend(handles=legend_elements, fontsize=6)

    plt.tight_layout()
    plt.savefig(output)
    plt.close(fig)
    print(f"  Saved → {output}")


# ============================================================
# Main
# ============================================================

def main():
    assert TEST_CONFIGS or ABLATION_CONFIGS, "Nothing to plot."

    for cfg in TEST_CONFIGS:
        plot_one_test(cfg, DIRECTORIES)

    for cfg in ABLATION_CONFIGS:
        plot_ablation_test(cfg, DIRECTORIES)

    print("\nDone.")


if __name__ == "__main__":
    main()