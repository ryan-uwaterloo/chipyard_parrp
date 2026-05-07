#!/usr/bin/env python3

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scienceplots
from matplotlib.patches import Patch
from scipy.stats import gaussian_kde
from collections import defaultdict

plt.style.use(['science', 'ieee'])

# ============================================================
# Configuration (Hard-coded for paper reproducibility)
# ============================================================

DATA_DIR = "../parsed/synthetics"
DATA_START = 50_000

# List of test names — filenames are derived automatically:
#   L1:          {DATA_DIR}/{test}-l1-{ctrl,parrp}.csv
#   L1-release:  {DATA_DIR}/{test}-l1-{ctrl,parrp}_releases.csv
#   LLC:         {DATA_DIR}/{test}-{ctrl,parrp}.csv
TESTS = [
    "probe-4",
    "relbuf-4",
    "nmshrs-4",
    "hol-4",
    # "other-test",
]

# Labels shown on the x-axis, one per test
TEST_LABELS = [
    "Probe",
    "RelBuf",
    "nMSHRs",
    "HoL",
    # "Other Test",
]

# Toggle which subplots to generate
PLOT_MISS_PENALTY   = True
PLOT_EVICTION_TIME  = True
PLOT_RESIDUAL       = True
PLOT_PROBE_LATENCY  = True

OUTPUT_FILE = "combined_violin_ieee_synth.svg"

CHUNK_SIZE = 5_000_000


# ============================================================
# IEEE Figure Style
# ============================================================

plt.rcParams.update({
    "text.usetex": False,
    "font.family": "serif",
})

SUBPLOT_CONFIGS = {
    "miss_penalty": {
        "enabled":  lambda: PLOT_MISS_PENALTY,
        "ylabel":   "Miss Penalty (cycles)",
        "title":    "Miss Penalty Distribution",
    },
    "eviction_time": {
        "enabled":  lambda: PLOT_EVICTION_TIME,
        "ylabel":   "Eviction Time (cycles)",
        "title":    "Eviction Time Distribution",
    },
    "residual": {
        "enabled":  lambda: PLOT_RESIDUAL,
        "ylabel":   "SourceD-to-Complete (cycles)",
        "title":    "LLC Release Residual Distribution",
    },
    "probe_latency": {
        "enabled":  lambda: PLOT_PROBE_LATENCY,
        "ylabel":   "Probe Latency (cycles)",
        "title":    "Probe Latency Distribution",
    },
}


# ============================================================
# Path helpers
# ============================================================

def paths(test):
    return {
        "l1_ctrl":       f"{DATA_DIR}/{test}-l1-ctrl.csv",
        "l1_parrp":      f"{DATA_DIR}/{test}-l1-parrp.csv",
        "rel_ctrl":      f"{DATA_DIR}/{test}-l1-ctrl_releases.csv",
        "rel_parrp":     f"{DATA_DIR}/{test}-l1-parrp_releases.csv",
        "llc_ctrl":      f"{DATA_DIR}/{test}-ctrl.csv",
        "llc_parrp":     f"{DATA_DIR}/{test}-parrp.csv",
        "probe_ctrl":    f"{DATA_DIR}/{test}-ctrl-probes.csv",
        "probe_parrp":   f"{DATA_DIR}/{test}-parrp-probes.csv",
    }


# ============================================================
# Loaders
# ============================================================

def load_miss_penalty(filepath):
    data_chunks = []
    global_min = float("inf")
    global_max = float("-inf")

    for chunk in pd.read_csv(filepath, usecols=["MissPenalty", "StartCycle"], chunksize=CHUNK_SIZE):
        values = chunk["MissPenalty"].to_numpy(dtype=np.uint16)
        start_cycles = chunk["StartCycle"].to_numpy(dtype=np.uint64)
        mask = (start_cycles >= DATA_START) & (values >= 10)
        values = values[mask]
        if len(values) == 0:
            continue
        data_chunks.append(values)
        global_min = min(global_min, values.min())
        global_max = max(global_max, values.max())

    full_array = np.concatenate(data_chunks)
    return full_array, global_min, global_max


def load_eviction_time(release_filepath, l1_filepath, num_sets=64, chunk_size=CHUNK_SIZE):
    assert (num_sets & (num_sets - 1)) == 0, f"num_sets must be a power of 2, got {num_sets}"

    def index_bits(addr_series):
        ints = np.array([int(x, 16) for x in addr_series], dtype=np.int64)
        return ((ints >> 6) & (num_sets - 1)).astype(np.int32)

    l1 = pd.read_csv(l1_filepath, usecols=["Address", "Core", "StartCycle", "EndCycle"])
    l1["Core"] = l1["Core"].str.lower()
    l1["IndexBits"] = index_bits(l1["Address"])
    l1["TargetCycle"] = (l1["EndCycle"] - 3).astype(np.int32)
    l1 = l1[["Core", "IndexBits", "StartCycle", "EndCycle", "TargetCycle"]].rename(
        columns={"StartCycle": "l1_start", "EndCycle": "l1_end"}
    )

    # Sort l1 so we can use searchsorted instead of a join
    l1 = l1.sort_values(["Core", "IndexBits", "l1_start"]).reset_index(drop=True)

    results = []
    global_min = float("inf")
    global_max = float("-inf")

    for chunk in pd.read_csv(
        release_filepath,
        usecols=["Address", "Core", "StartCycle"],
        chunksize=chunk_size,
    ):
        chunk = chunk[chunk["StartCycle"] >= DATA_START].copy()
        chunk["Core"] = chunk["Core"].str.lower()
        chunk["IndexBits"] = index_bits(chunk["Address"])

        eviction_times = []

        for (core, idx_bits), rel_group in chunk.groupby(["Core", "IndexBits"]):
            l1_group = l1[(l1["Core"] == core) & (l1["IndexBits"] == idx_bits)]
            if l1_group.empty:
                continue

            starts = l1_group["l1_start"].to_numpy()
            ends   = l1_group["l1_end"].to_numpy()
            targets = l1_group["TargetCycle"].to_numpy()

            rel_cycles = rel_group["StartCycle"].to_numpy()

            # For each release cycle, find candidate l1 intervals via searchsorted
            pos = np.searchsorted(starts, rel_cycles, side="right") - 1

            for j, (cycle, p) in enumerate(zip(rel_cycles, pos)):
                if p < 0:
                    continue
                if starts[p] <= cycle <= ends[p]:
                    eviction_times.append(targets[p] - cycle)
                    # print(f"{cycle}, {targets[p] - cycle}")

        if eviction_times:
            results.append(np.array(eviction_times, dtype=np.int32))
            global_min = min(global_min, min(eviction_times))
            global_max = max(global_max, max(eviction_times))

    full_array = np.concatenate(results) if results else np.array([], dtype=np.int32)
    return full_array, global_min, global_max

def load_eviction_time_ctrl(release_filepath, chunk_size=CHUNK_SIZE):
    chunks = []
    global_min = float("inf")
    global_max = float("-inf")
    for chunk in pd.read_csv(release_filepath, usecols=["Latency", "StartCycle"], chunksize=chunk_size):
        mask = chunk["StartCycle"] >= DATA_START
        values = chunk.loc[mask, "Latency"].to_numpy(dtype=np.int32)
        chunks.append(values)

        if values.any():
            global_min = min(global_min, values.min())
            global_max = max(global_max, values.max())
        
    full_array = np.concatenate(chunks) if any(len(c) > 0 for c in chunks) else np.array([], dtype=np.int32)
    return full_array, global_min, global_max


def load_source_d_to_complete(llc_filepath, chunk_size=CHUNK_SIZE):
    RELEASE_OPCODES = {"Release", "ReleaseData"}
    chunks = []
    for chunk in pd.read_csv(llc_filepath, usecols=["Opcode", "SourceDToComplete", "StartCycle"], chunksize=chunk_size):
        mask = chunk["Opcode"].str.strip().isin(RELEASE_OPCODES) & (chunk["StartCycle"] >= DATA_START)
        values = pd.to_numeric(chunk.loc[mask, "SourceDToComplete"], errors="coerce").dropna()
        chunks.append(values.to_numpy(dtype=np.int32))
    return np.concatenate(chunks) if any(len(c) > 0 for c in chunks) else np.array([], dtype=np.int32)

def load_probe_latency(probe_filepath, chunk_size=CHUNK_SIZE):
    chunks = []
    for chunk in pd.read_csv(probe_filepath, usecols=["Latency", "StartCycle"], chunksize=chunk_size):
        mask = chunk["StartCycle"] >= 10_000
        values = pd.to_numeric(chunk.loc[mask, "Latency"], errors="coerce").dropna()
        chunks.append(values.to_numpy(dtype=np.int32))
    return np.concatenate(chunks) if any(len(c) > 0 for c in chunks) else np.array([], dtype=np.int32)


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

    if side == 'left':
        ax.fill_betweenx(y_vals, x_center - density, x_center,
                         facecolor='white', edgecolor=color, hatch=hatch, alpha=1, linewidth=0.5)
    else:
        ax.fill_betweenx(y_vals, x_center, x_center + density,
                         facecolor='white', edgecolor=color, hatch=hatch, alpha=1, linewidth=0.5)


def is_degenerate(data, tol=1e-6):
    return len(data) == 0 or np.ptp(data) < tol


def draw_violin_pair(ax, data_ctrl, data_parrp, x_pos, half_width=0.35, gap=0.01):
    has_ctrl  = len(data_ctrl)  > 0 and not is_degenerate(data_ctrl)
    has_parrp = len(data_parrp) > 0 and not is_degenerate(data_parrp)

    if not has_ctrl and not has_parrp:
        # Could be no data, or all data degenerate — show the value if we have it
        for data, xoff, ha in [(data_ctrl, -gap, 'right'), (data_parrp, gap, 'left')]:
            if len(data) > 0:
                ax.axhline(np.mean(data), color='gray', linewidth=0.5, linestyle='--')
                ax.text(x_pos + xoff, np.mean(data), f"{np.mean(data):.1f}",
                        ha=ha, va='bottom', fontsize=5, color='gray')
            else:
                ax.text(x_pos + xoff, 0.5, "No data", ha=ha, va='center',
                        transform=ax.get_xaxis_transform(), fontsize=6, color='gray')
        return

    if has_ctrl and has_parrp:
        shared_area = min(
            compute_max_area(data_ctrl,  half_width),
            compute_max_area(data_parrp, half_width),
        )
        area_ctrl  = shared_area
        area_parrp = shared_area
    elif has_ctrl:
        area_ctrl  = compute_max_area(data_ctrl,  half_width)
        area_parrp = None
    else:
        area_ctrl  = None
        area_parrp = compute_max_area(data_parrp, half_width)

    if has_ctrl:
        make_half_violin(ax, data_ctrl,  x_pos - gap, 'left',  'red',  '////////', area_ctrl)
        ax.hlines(np.mean(data_ctrl),  x_pos - half_width, x_pos - gap, linewidth=0.8, color='k')
    else:
        # Degenerate — draw a spike at the single value
        val = np.mean(data_ctrl)
        ax.hlines(val, x_pos - half_width, x_pos - gap, linewidth=0.8, color='k', linestyle='--')
        ax.text(x_pos - gap, val, f"{val:.1f}", ha='right', va='bottom', fontsize=5, color='red')

    if has_parrp:
        make_half_violin(ax, data_parrp, x_pos + gap, 'right', 'blue', 'xxxxxxxx', area_parrp)
        ax.hlines(np.mean(data_parrp), x_pos + gap, x_pos + half_width, linewidth=0.8, color='r')
    else:
        # Degenerate — draw a spike at the single value
        val = np.mean(data_parrp)
        ax.hlines(val, x_pos + gap, x_pos + half_width, linewidth=0.8, color='r', linestyle='--')
        ax.text(x_pos + gap, val, f"{val:.1f}", ha='left', va='bottom', fontsize=5, color='blue')


# ============================================================
# Main
# ============================================================

def main():
    # Which subplots are active
    active = [k for k, v in SUBPLOT_CONFIGS.items() if v["enabled"]()]
    n_plots = len(active)
    assert n_plots > 0, "No subplots enabled."
    assert len(TESTS) == len(TEST_LABELS), "TESTS and TEST_LABELS must be the same length."

    x = np.arange(1, len(TESTS) + 1)

    # Load all data upfront, per test
    test_data = {}   # test_name -> { "miss_penalty": (ctrl, parrp), "eviction_time": ..., "residual": ... }

    for test in TESTS:
        p = paths(test)
        print(f"\nLoading data for test: {test}")
        entry = {}

        if PLOT_MISS_PENALTY:
            print("  Loading miss penalty (ctrl)...")
            entry["miss_penalty_ctrl"],  entry["min_miss_ctrl"], entry["max_miss_ctrl"] = load_miss_penalty(p["l1_ctrl"])
            print(f"{entry["min_miss_ctrl"]}, {entry["max_miss_ctrl"]}")
            print("  Loading miss penalty (parrp)...")
            entry["miss_penalty_parrp"], entry["min_miss_parrp"], entry["max_miss_parrp"] = load_miss_penalty(p["l1_parrp"])
            print(f"{entry["min_miss_parrp"]}, {entry["max_miss_parrp"]}")

        if PLOT_EVICTION_TIME:
            num_sets = 128 if "moresets" in test else 64
            print("  Loading eviction time (ctrl)...")
            entry["eviction_time_ctrl"], entry["min_evict_ctrl"], entry["max_evict_ctrl"]  = load_eviction_time_ctrl(p["rel_ctrl"])
            print(f"{entry["min_evict_ctrl"]}, {entry["max_evict_ctrl"]}")
            print("  Loading eviction time (parrp)...")
            entry["eviction_time_parrp"], entry["min_evict_parrp"], entry["max_evict_parrp"] = load_eviction_time(p["rel_parrp"], p["l1_parrp"], num_sets)
            print(f"{entry["min_evict_parrp"]}, {entry["max_evict_parrp"]}")

        if PLOT_RESIDUAL:
            print("  Loading LLC residual (ctrl)...")
            entry["residual_ctrl"]  = load_source_d_to_complete(p["llc_ctrl"])
            print("  Loading LLC residual (parrp)...")
            entry["residual_parrp"] = load_source_d_to_complete(p["llc_parrp"])

        if PLOT_PROBE_LATENCY:
            print("  Loading probe latency (ctrl)...")
            entry["probe_latency_ctrl"]  = load_probe_latency(p["probe_ctrl"])
            print("  Loading probe latency (parrp)...")
            entry["probe_latency_parrp"] = load_probe_latency(p["probe_parrp"])

        test_data[test] = entry

    print("\nGenerating plot...")

    fig, axes = plt.subplots(1, n_plots, figsize=(3.5 * n_plots, 3))
    if n_plots == 1:
        axes = [axes]

    data_keys = {
        "miss_penalty":  ("miss_penalty_ctrl",  "miss_penalty_parrp"),
        "eviction_time": ("eviction_time_ctrl",  "eviction_time_parrp"),
        "residual":      ("residual_ctrl",        "residual_parrp"),
        "probe_latency": ("probe_latency_ctrl", "probe_latency_parrp"),
    }

    for ax, subplot_key in zip(axes, active):
        cfg = SUBPLOT_CONFIGS[subplot_key]
        ctrl_key, parrp_key = data_keys[subplot_key]

        for i, test in enumerate(TESTS):
            draw_violin_pair(
                ax,
                test_data[test][ctrl_key],
                test_data[test][parrp_key],
                x_pos=x[i],
            )

        ax.set_xticks(x)
        ax.set_xticklabels(TEST_LABELS)
        ax.set_ylabel(cfg["ylabel"])
        ax.set_title(cfg["title"])
        ax.grid(axis="y", linestyle="--", linewidth=0.5)

    legend_elements = [
        Patch(facecolor="white", edgecolor="red",  hatch="////////", label="Stock"),
        Patch(facecolor="white", edgecolor="blue", hatch="xxxxxxxx", label="Parrp"),
    ]
    axes[0].legend(handles=legend_elements)

    plt.rcParams["hatch.linewidth"] = 0.2
    plt.tight_layout()
    plt.savefig(OUTPUT_FILE)
    plt.close(fig)

    print(f"\nSaved figure to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()