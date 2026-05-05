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

DATA_DIR = "../parsed/synth"

# List of test names — filenames are derived automatically:
#   L1:          {DATA_DIR}/{test}-l1-{ctrl,parrp}.csv
#   L1-release:  {DATA_DIR}/{test}-l1-{ctrl,parrp}_releases.csv
#   LLC:         {DATA_DIR}/{test}-{ctrl,parrp}.csv
TESTS = [
    "nmshrs",
    "probe",
    "relbuf",
    "hol",
    # "other-test",
]

# Labels shown on the x-axis, one per test
TEST_LABELS = [
    "nMSHRs",
    "Probe",
    "ReleaseBuffer",
    "HoL"
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
        mask = (start_cycles >= 10_000) & (values >= 10)
        values = values[mask]
        if len(values) == 0:
            continue
        data_chunks.append(values)
        global_min = min(global_min, values.min())
        global_max = max(global_max, values.max())

    full_array = np.concatenate(data_chunks)
    return full_array, global_min, global_max


def load_eviction_time(release_filepath, l1_filepath, chunk_size=CHUNK_SIZE):
    def index_bits(addr_series):
        return addr_series.apply(lambda x: (int(x, 16) >> 6) & 0x3F)

    l1 = pd.read_csv(l1_filepath, usecols=["Address", "Core", "StartCycle", "EndCycle"])
    l1["Core"] = l1["Core"].str.lower()
    l1["IndexBits"] = index_bits(l1["Address"])
    l1["TargetCycle"] = l1["EndCycle"] - 3

    lookup = defaultdict(list)
    for row in l1.itertuples(index=False):
        lookup[(row.Core, row.IndexBits)].append((row.StartCycle, row.EndCycle, row.TargetCycle))

    eviction_times = []

    for chunk in pd.read_csv(release_filepath, usecols=["Address", "Core", "StartCycle"], chunksize=chunk_size):
        chunk = chunk[chunk["StartCycle"] >= 10_000]
        chunk["Core"] = chunk["Core"].str.lower()
        chunk["IndexBits"] = index_bits(chunk["Address"])

        for row in chunk.itertuples(index=False):
            candidates = lookup.get((row.Core, row.IndexBits), [])
            best, best_dist = None, float("inf")
            for (l1_start, l1_end, target) in candidates:
                if l1_start <= row.StartCycle <= l1_end:
                    dist = abs(row.StartCycle - l1_start)
                    if dist < best_dist:
                        best_dist, best = dist, target
            if best is not None:
                eviction_times.append(best - row.StartCycle)

    return np.array(eviction_times, dtype=np.int32)

def load_eviction_time_ctrl(release_filepath, chunk_size=CHUNK_SIZE):
    chunks = []
    for chunk in pd.read_csv(release_filepath, usecols=["Latency", "StartCycle"], chunksize=chunk_size):
        mask = chunk["StartCycle"] >= 10_000
        chunks.append(chunk.loc[mask, "Latency"].to_numpy(dtype=np.int32))
    return np.concatenate(chunks) if any(len(c) > 0 for c in chunks) else np.array([], dtype=np.int32)


def load_source_d_to_complete(llc_filepath, chunk_size=CHUNK_SIZE):
    RELEASE_OPCODES = {"Release", "ReleaseData"}
    chunks = []
    for chunk in pd.read_csv(llc_filepath, usecols=["Opcode", "SourceDToComplete", "StartCycle"], chunksize=chunk_size):
        mask = chunk["Opcode"].str.strip().isin(RELEASE_OPCODES) & (chunk["StartCycle"] >= 10_000)
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


def draw_violin_pair(ax, data_ctrl, data_parrp, x_pos, half_width=0.35, gap=0.01):
    has_ctrl  = len(data_ctrl)  > 0
    has_parrp = len(data_parrp) > 0

    if not has_ctrl and not has_parrp:
        ax.text(x_pos, 0.5, "No data", ha='center', va='center',
                transform=ax.get_xaxis_transform(), fontsize=6, color='gray')
        return

    # Compute areas independently if one side is missing
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
        ax.text(x_pos - gap, 0.5, "N/A", ha='right', va='center',
                transform=ax.get_xaxis_transform(), fontsize=5, color='gray')

    if has_parrp:
        make_half_violin(ax, data_parrp, x_pos + gap, 'right', 'blue', 'xxxxxxxx', area_parrp)
        ax.hlines(np.mean(data_parrp), x_pos + gap, x_pos + half_width, linewidth=0.8, color='r')
    else:
        ax.text(x_pos + gap, 0.5, "N/A", ha='left', va='center',
                transform=ax.get_xaxis_transform(), fontsize=5, color='gray')


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
            entry["miss_penalty_ctrl"],  _, _ = load_miss_penalty(p["l1_ctrl"])
            print("  Loading miss penalty (parrp)...")
            entry["miss_penalty_parrp"], _, _ = load_miss_penalty(p["l1_parrp"])

        if PLOT_EVICTION_TIME:
            print("  Loading eviction time (ctrl)...")
            entry["eviction_time_ctrl"]  = load_eviction_time_ctrl(p["rel_ctrl"])
            print("  Loading eviction time (parrp)...")
            entry["eviction_time_parrp"] = load_eviction_time(p["rel_parrp"], p["l1_parrp"])

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