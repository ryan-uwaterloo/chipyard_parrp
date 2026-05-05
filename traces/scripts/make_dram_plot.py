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

# List of test names — filenames are derived automatically:
#   L1:          {DATA_DIR}/{test}-l1-{ctrl,parrp}.csv
#   L1-release:  {DATA_DIR}/{test}-l1-{ctrl,parrp}_releases.csv
#   LLC:         {DATA_DIR}/{test}-{ctrl,parrp}.csv
TESTS = [
    # "radix-4",
    # "other-test",
    "probe-4",
    "relbuf-4",
    "nmshrs-4",
    "hol-4",
]

# Labels shown on the x-axis, one per test
TEST_LABELS = [
    # "Radix",
    # "Other Test",
    "Probe",
    "RelBuf",
    "nMSHRs",
    "HoL",
]

# Toggle which subplots to generate

OUTPUT_FILE = "dram_ieee_synth.svg"

CHUNK_SIZE = 5_000_000


# ============================================================
# IEEE Figure Style
# ============================================================

plt.rcParams.update({
    "text.usetex": False,
    "font.family": "serif",
})

DRAM = True

SUBPLOT_CONFIGS = {
    "dram": {
        "enabled":  lambda: DRAM,
        "ylabel":   "DRAM Access Time (cycleS)",
        "title":    "DRMA Access Time",
    }
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
        "dram_ctrl":     f"{DATA_DIR}/{test}-ctrl-dram.csv",
        "dram_parrp":     f"{DATA_DIR}/{test}-parrp-dram.csv",
    }


# ============================================================
# Loaders
# ============================================================

def load_dram(filepath):
    data_chunks = []
    global_min = float("inf")
    global_max = float("-inf")

    for chunk in pd.read_csv(filepath, usecols=["Latency", "StartCycle"], chunksize=CHUNK_SIZE):
        values = chunk["Latency"].to_numpy(dtype=np.uint16)
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

        print("  Loading LLC residual (ctrl)...")
        entry["residual_ctrl"], _, _  = load_dram(p["dram_ctrl"])
        print("  Loading LLC residual (parrp)...")
        entry["residual_parrp"], _, _ = load_dram(p["dram_parrp"])

        test_data[test] = entry

    print("\nGenerating plot...")

    fig, axes = plt.subplots(1, n_plots, figsize=(3.5 * n_plots, 3))
    if n_plots == 1:
        axes = [axes]

    data_keys = {
        "dram":      ("residual_ctrl",        "residual_parrp"),
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