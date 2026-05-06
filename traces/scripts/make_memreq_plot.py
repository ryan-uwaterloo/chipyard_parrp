#!/usr/bin/env python3

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scienceplots
from matplotlib.patches import Patch
from scipy.stats import gaussian_kde
from pathlib import Path

plt.style.use(['science', 'ieee'])

# ============================================================
# Configuration
# ============================================================

DATA_DIR = "../parsed/synthetics"
# DATA_DIR    = "../parsed"
MEMREQ_DIR  = "../mrt_data"

TESTS = [
    # ("radiosity-4", "Radiosity"),
    # ("radix-4", "Radix"),
    # ("barnes-4", "Barnes"),
    # ("water-nsquared-4", "Water-N^2"),
    ("probe-4", "Probe"),
    ("relbuf-4", "RelBuf"),
    ("nmshrs-4", "nMSHRs"),
    ("hol-4", "HoL"),
]

STOCK_SUFFIX    = "-ctrl"
MODIFIED_SUFFIX = "-parrp"

CORES       = [0, 1, 2, 3]
CACHE_TYPES = ["data", "inst"]

PLOT_LOADS  = True
PLOT_STORES = True

OUTPUT_FILE      = "reqtime_violin_synth.svg"
HIT_MISS_CSV_OUT = "hit_miss_summary_synth.csv"

CHUNK_SIZE  = 5_000_000
MIN_SAMPLES = 30

# ============================================================
# Static SourceID -> (core, cache_type) map
#
# Derived from TLMasterParameters dump.  Within each core the *lower*
# IdRange pair is the ICache and the *higher* IdRange pair is the DCache.
# Each cache has a companion MMIO range immediately above it.
# ICache + ICache MMIO -> "inst";  DCache + DCache MMIO -> "data"
# (matching the memreqtime filename tokens used in MEMREQ_DIR).
#
#   Core 0:  inst: 48-53  data: 56-62
#   Core 1:  inst: 32-38  data: 40-46
#   Core 2:  inst: 16-22  data: 24-30
#   Core 3:  inst:  0-6   data:  8-14
#
# IdRange(lo, hi) is exclusive on the upper bound (Chisel/Rocket convention).
# ============================================================

def _build_source_id_map() -> dict[int, tuple[int, str]]:
    """Return dict: integer source ID -> (core, cache_type).
    cache_type is 'inst' (ICache + ICache MMIO) or 'data' (DCache + DCache MMIO),
    matching the memreqtime filename tokens used in MEMREQ_DIR.
    """
    # (core, lo, hi_exclusive, cache_type)
    # ICache and ICache MMIO both map to "inst"; DCache and DCache MMIO to "data".
    ranges = [
        # Core 0
        (0, 48, 53, "inst"),   # ICache
        (0, 53, 54, "inst"),   # ICache MMIO
        (0, 56, 61, "data"),   # DCache
        (0, 61, 62, "data"),   # DCache MMIO
        # Core 1
        (1, 32, 37, "inst"),
        (1, 37, 38, "inst"),
        (1, 40, 45, "data"),
        (1, 45, 46, "data"),
        # Core 2
        (2, 16, 21, "inst"),
        (2, 21, 22, "inst"),
        (2, 24, 29, "data"),
        (2, 29, 30, "data"),
        # Core 3
        (3,  0,  5, "inst"),
        (3,  5,  6, "inst"),
        (3,  8, 13, "data"),
        (3, 13, 14, "data"),
    ]
    mapping: dict[int, tuple[int, str]] = {}
    for core, lo, hi, ctype in ranges:
        for sid in range(lo, hi):
            if sid in mapping:
                raise ValueError(
                    f"Overlapping source ID {sid}: already mapped to "
                    f"{mapping[sid]}, tried to add (core={core}, {ctype})"
                )
            mapping[sid] = (core, ctype)
    return mapping


SOURCE_ID_MAP: dict[int, tuple[int, str]] = _build_source_id_map()

# Opcodes that represent an LLC-level miss (Sink A requests)
ACQUIRE_OPCODES = {"AcquireBlock", "AcquirePerm"}

# ============================================================
# IEEE style tweaks
# ============================================================

plt.rcParams.update({
    "text.usetex": False,
    "font.family": "serif",
    "hatch.linewidth": 0.2,
})

# ============================================================
# Path helpers
# ============================================================

def find_csvs(test_variant: str, cache_type: str) -> list[Path]:
    """Return all per-core memreqtime CSV paths for a given test variant + cache type.
    Files live in MEMREQ_DIR and are named e.g. radix-4-parrp.0_data.csv
    """
    found = []
    for core in CORES:
        p = Path(MEMREQ_DIR) / f"{test_variant}.{core}_{cache_type}.csv"
        if p.exists():
            found.append(p)
    return found


def mrt_total_counts(test_name: str, variant: str) -> dict[tuple[int, str], int]:
    """Count total L1 requests per (core, cache_type) from memreqtime CSVs.
    Core and cache_type are read directly from the filename — no source ID
    mapping required.  Returns {(core, cache_type): row_count}.
    """
    counts: dict[tuple[int, str], int] = {}
    test_variant = f"{test_name}-{variant}"
    for cache_type in CACHE_TYPES:
        for core in CORES:
            p = Path(MEMREQ_DIR) / f"{test_variant}.{core}_{cache_type}.csv"
            if not p.exists():
                continue
            # Count data rows without loading all columns
            n = sum(1 for _ in open(p)) - 1   # subtract header
            counts[(core, cache_type)] = max(0, n)
            print(f"  mrt totals: core={core} {cache_type}: {counts[(core, cache_type)]} rows  ({p.name})")
    return counts


def llc_csv_path(test_name: str, variant: str) -> Path:
    """Path to the LLC completions CSV produced by parse_log.py --csv.
    e.g. ../parsed/radiosity-4-ctrl.csv
    """
    return Path(DATA_DIR) / f"{test_name}-{variant}.csv"



# ============================================================
# Hit / Miss correlation
# ============================================================

def _parse_source_id(raw) -> int | None:
    """Parse a SourceID cell ('0x38', '56', 56, ...) to int. Returns None on failure."""
    try:
        s = str(raw).strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (ValueError, TypeError):
        return None


def compute_hit_miss(test_name: str, variant: str) -> pd.DataFrame:
    """
    Compute per-(core, cache_type) hit/miss statistics for one test variant.

    Miss counting  (LLC Sink A)
    ---------------------------
    Each row in the LLC CSV with Opcode in {AcquireBlock, AcquirePerm}
    is one LLC miss.  SourceID is resolved to (core, cache_type) via
    SOURCE_ID_MAP.
    File: {DATA_DIR}/{test_name}-{variant}.csv  (e.g. radiosity-4-ctrl.csv)

    Total-request counting  (memreqtime CSVs)
    ------------------------------------------
    Row count of each per-(core, cache_type) memreqtime file.  Core and
    cache_type are encoded in the filename so no source ID mapping is needed.
    Files: {MEMREQ_DIR}/{test_name}-{variant}.{core}_{cache_type}.csv

    Hit rate
    --------
    hits     = max(0, total_requests - misses)   # clamp for log truncation
    hit_rate = hits / total_requests             # NaN when total == 0

    Returns
    -------
    DataFrame with columns:
        core, cache_type, misses, total_requests, hits, hit_rate
    """
    miss_counts:  dict[tuple[int, str], int] = {}

    # --- LLC misses ---
    llc_path = llc_csv_path(test_name, variant)
    if llc_path.exists():
        llc_df = pd.read_csv(llc_path, usecols=["SourceID", "Opcode"])
        acquire_mask = llc_df["Opcode"].isin(ACQUIRE_OPCODES)
        for raw_sid in llc_df.loc[acquire_mask, "SourceID"]:
            sid = _parse_source_id(raw_sid)
            if sid is None:
                continue
            key = SOURCE_ID_MAP.get(sid)
            if key is None:
                print(f"  [warn] LLC miss: unmapped SourceID {raw_sid!r} (int={sid})")
                continue
            miss_counts[key] = miss_counts.get(key, 0) + 1
    else:
        print(f"  [warn] LLC CSV not found: {llc_path}")

    # --- Totals from memreqtime files ---
    total_counts = mrt_total_counts(test_name, variant)

    # --- Build result rows for every known (core, cache_type) bucket ---
    all_keys = sorted(set(miss_counts) | set(total_counts))
    rows = []
    for (core, ctype) in all_keys:
        misses   = miss_counts.get((core, ctype), 0)
        total    = total_counts.get((core, ctype), 0)
        hits     = max(0, total - misses)
        hit_rate = (hits / total) if total > 0 else float("nan")
        rows.append({
            "core":           core,
            "cache_type":     ctype,
            "misses":         misses,
            "total_requests": total,
            "hits":           hits,
            "hit_rate":       hit_rate,
        })

    return pd.DataFrame(rows, columns=["core", "cache_type", "misses",
                                        "total_requests", "hits", "hit_rate"])


def print_hit_miss_table(df: pd.DataFrame) -> None:
    """Pretty-print a hit/miss summary table for one test variant."""
    print(f"  {'core':<6} {'cache_type':<14} {'misses':>8} {'total':>8} "
          f"{'hits':>8} {'hit_rate':>9}")
    print(f"  {'-'*6} {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*9}")
    for _, row in df.sort_values(["core", "cache_type"]).iterrows():
        hr = f"{row['hit_rate']:.2%}" if not pd.isna(row["hit_rate"]) else "   N/A"
        print(f"  {int(row['core']):<6} {row['cache_type']:<14} "
              f"{int(row['misses']):>8} {int(row['total_requests']):>8} "
              f"{int(row['hits']):>8} {hr:>9}")


# ============================================================
# Loader  (reqtime violin data)
# ============================================================

def load_reqtimes(test_variant: str, cache_type: str) -> dict[str, np.ndarray]:
    """
    Aggregate reqTime across all cores for a given test variant + cache_type.
    Returns {"Load": array, "Store": array}.
    """
    accum: dict[str, list[np.ndarray]] = {"Load": [], "Store": []}

    csvs = find_csvs(test_variant, cache_type)
    if not csvs:
        print(f"  [warn] No reqtime files for {test_variant} / {cache_type}")
        return {k: np.array([], dtype=np.float32) for k in accum}

    for path in csvs:
        print(f"  Reading {path.name} ...")
        for chunk in pd.read_csv(path, usecols=["nodeType", "reqTime"],
                                 chunksize=CHUNK_SIZE):
            chunk = chunk.dropna(subset=["reqTime"])
            chunk["reqTime"] = pd.to_numeric(chunk["reqTime"], errors="coerce")
            chunk = chunk.dropna(subset=["reqTime"])
            for node_type in ("Load", "Store"):
                mask = chunk["nodeType"].str.strip() == node_type
                vals = chunk.loc[mask, "reqTime"].to_numpy(dtype=np.float32)
                if len(vals):
                    accum[node_type].append(vals)

    return {
        k: np.concatenate(v) if v else np.array([], dtype=np.float32)
        for k, v in accum.items()
    }


# ============================================================
# Violin helpers
# ============================================================

def compute_max_area(data, half_width, bw_method=0.4):
    kde = gaussian_kde(data, bw_method=bw_method)
    y_vals = np.linspace(data.min(), data.max(), 500)
    density = kde(y_vals)
    area = np.trapezoid(density, y_vals)
    return half_width * area / density.max()


def make_half_violin(ax, data, x_center, side, color, hatch, max_area, bw_method=0.4):
    kde = gaussian_kde(data, bw_method=bw_method)
    y_vals = np.linspace(data.min(), data.max(), 500)
    density = kde(y_vals)
    area = np.trapezoid(density, y_vals)
    density = density / area * max_area

    if side == "left":
        ax.fill_betweenx(y_vals, x_center - density, x_center,
                         facecolor="white", edgecolor=color, hatch=hatch,
                         alpha=1, linewidth=0.5)
    else:
        ax.fill_betweenx(y_vals, x_center, x_center + density,
                         facecolor="white", edgecolor=color, hatch=hatch,
                         alpha=1, linewidth=0.5)


def draw_violin_pair(ax, data_stock, data_mod, x_pos, half_width=0.35, gap=0.01):
    has_stock = len(data_stock) >= MIN_SAMPLES
    has_mod   = len(data_mod)   >= MIN_SAMPLES

    if not has_stock and not has_mod:
        ax.text(x_pos, 0.5, "No data", ha="center", va="center",
                transform=ax.get_xaxis_transform(), fontsize=6, color="gray")
        return

    if has_stock and has_mod:
        shared     = min(compute_max_area(data_stock, half_width),
                         compute_max_area(data_mod,   half_width))
        area_stock = area_mod = shared
    elif has_stock:
        area_stock = compute_max_area(data_stock, half_width)
        area_mod   = None
    else:
        area_stock = None
        area_mod   = compute_max_area(data_mod, half_width)

    if has_stock:
        make_half_violin(ax, data_stock, x_pos - gap, "left",
                         "red", "////////", area_stock)
        ax.hlines(np.mean(data_stock), x_pos - half_width, x_pos - gap,
                  linewidth=0.8, color="k")
    else:
        ax.text(x_pos - gap, 0.5, "N/A", ha="right", va="center",
                transform=ax.get_xaxis_transform(), fontsize=5, color="gray")

    if has_mod:
        make_half_violin(ax, data_mod, x_pos + gap, "right",
                         "blue", "xxxxxxxx", area_mod)
        ax.hlines(np.mean(data_mod), x_pos + gap, x_pos + half_width,
                  linewidth=0.8, color="r")
    else:
        ax.text(x_pos + gap, 0.5, "N/A", ha="left", va="center",
                transform=ax.get_xaxis_transform(), fontsize=5, color="gray")


# ============================================================
# Subplot configuration
# ============================================================

SUBPLOT_DEFS = []
if PLOT_LOADS:
    SUBPLOT_DEFS.append({
        "title":       "Load Request Time",
        "ylabel":      "Request Time (cycles)",
        "node_type":   "Load",
        "cache_types": ["data", "inst"],
    })
if PLOT_STORES:
    SUBPLOT_DEFS.append({
        "title":       "Store Request Time",
        "ylabel":      "Request Time (cycles)",
        "node_type":   "Store",
        "cache_types": ["data"],
    })


# ============================================================
# Main
# ============================================================

def main():
    assert SUBPLOT_DEFS, "No subplots enabled — set PLOT_LOADS / PLOT_STORES to True."
    assert TESTS, "TESTS list is empty."

    test_names  = [t[0] for t in TESTS]
    test_labels = [t[1] for t in TESTS]
    x           = np.arange(1, len(TESTS) + 1)

    # ---- Hit / miss correlation ---------------------------------------
    print("\n" + "=" * 60)
    print("Hit / Miss Correlation")
    print("=" * 60)

    all_hit_miss_rows = []

    for test_name, label in TESTS:
        for variant_suffix, variant_key in [(STOCK_SUFFIX, "stock"),
                                            (MODIFIED_SUFFIX, "mod")]:
            # variant_suffix is e.g. "-ctrl"; strip the leading dash for the filename
            variant_id = variant_suffix.lstrip("-")
            print(f"\n--- {label} / {variant_key} ({test_name}-{variant_id}) ---")
            df = compute_hit_miss(test_name, variant_id)
            print_hit_miss_table(df)

            df.insert(0, "variant",   variant_key)
            df.insert(0, "test_name", test_name)
            df.insert(0, "label",     label)
            all_hit_miss_rows.append(df)

    hit_miss_df = pd.concat(all_hit_miss_rows, ignore_index=True)
    hit_miss_df.to_csv(HIT_MISS_CSV_OUT, index=False, float_format="%.6f")
    print(f"\nHit/miss summary written to {HIT_MISS_CSV_OUT}")

    # ---- Load reqtime violin data ------------------------------------
    print("\n" + "=" * 60)
    print("Loading request-time data")
    print("=" * 60)

    # test_data[test_name][variant_key][cache_type] -> {"Load": arr, "Store": arr}
    test_data: dict = {}

    for test_name, label in TESTS:
        print(f"\n=== Loading: {label} ({test_name}) ===")
        test_data[test_name] = {}
        for variant_suffix, variant_key in [(STOCK_SUFFIX, "stock"),
                                            (MODIFIED_SUFFIX, "mod")]:
            variant_id = test_name + variant_suffix
            test_data[test_name][variant_key] = {}
            for cache_type in CACHE_TYPES:
                test_data[test_name][variant_key][cache_type] = \
                    load_reqtimes(variant_id, cache_type)

    # ---- Plot --------------------------------------------------------
    print("\nGenerating violin plot...")

    n_plots = len(SUBPLOT_DEFS)
    fig, axes = plt.subplots(1, n_plots, figsize=(3.5 * n_plots, 3))
    if n_plots == 1:
        axes = [axes]

    for ax, sdef in zip(axes, SUBPLOT_DEFS):
        node_type   = sdef["node_type"]
        cache_types = sdef["cache_types"]

        for i, test_name in enumerate(test_names):
            def aggregate(variant_key, _tn=test_name):
                parts = [
                    test_data[_tn][variant_key][ct][node_type]
                    for ct in cache_types
                ]
                parts = [p for p in parts if len(p) > 0]
                return np.concatenate(parts) if parts else np.array([], dtype=np.float32)

            draw_violin_pair(ax, aggregate("stock"), aggregate("mod"), x_pos=x[i])

        ax.set_xticks(x)
        ax.set_xticklabels(test_labels)
        ax.set_ylabel(sdef["ylabel"])
        ax.set_title(sdef["title"])
        ax.grid(axis="y", linestyle="--", linewidth=0.5)

    legend_elements = [
        Patch(facecolor="white", edgecolor="red",  hatch="////////", label="Stock"),
        Patch(facecolor="white", edgecolor="blue", hatch="xxxxxxxx", label="Modified"),
    ]
    axes[0].legend(handles=legend_elements)

    plt.tight_layout()
    plt.savefig(OUTPUT_FILE)
    plt.close(fig)
    print(f"Saved figure to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()