#!/usr/bin/env python3

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scienceplots

plt.style.use(['science', 'ieee'])

# ============================================================
# Configuration (Hard-coded for paper reproducibility)
# ============================================================

CSV_FILES = [
    "../parsed/new_radix-4-l1-ctrl.csv",
    "../parsed/new_fmm-4-l1-ctrl.csv",
]

LABELS = [
    "Radix-4",
    "FMM-4",
]

OUTPUT_FILE = "miss_penalty_boxplot_ieee.pdf"

CHUNK_SIZE = 5_000_000   # rows per chunk for streaming


# ============================================================
# IEEE Figure Style
# ============================================================

plt.rcParams.update({
    "text.usetex": False,
    "font.family": "serif",
})


# ============================================================
# Streaming Loader (Multi-GB Safe)
# ============================================================

def load_miss_penalty(filepath):
    """
    Stream-load only the MissPenalty column.
    Returns:
        numpy array of values
        true_min
        true_max
    """
    data_chunks = []
    global_min = float("inf")
    global_max = float("-inf")

    for chunk in pd.read_csv(
        filepath,
        usecols=["MissPenalty"],
        chunksize=CHUNK_SIZE
    ):
        values = chunk["MissPenalty"].to_numpy(dtype=np.float64)

        data_chunks.append(values)

        # Track true extrema during streaming
        local_min = values.min()
        local_max = values.max()

        if local_min < global_min:
            global_min = local_min
        if local_max > global_max:
            global_max = local_max

    full_array = np.concatenate(data_chunks)

    return full_array, global_min, global_max


# ============================================================
# Main
# ============================================================

def main():

    all_data = []
    mins = []
    maxs = []

    print("Loading CSV files...")

    for file in CSV_FILES:
        print(f"  Processing {file} ...")
        values, mn, mx = load_miss_penalty(file)

        all_data.append(values)
        mins.append(mn)
        maxs.append(mx)

        print(f"    Min: {mn}")
        print(f"    Max: {mx}")
        print(f"    Samples: {len(values)}")

    print("Generating plot...")

    fig, ax = plt.subplots()

    # True min/max whiskers
    box = ax.boxplot(
        all_data,
        whis=[0, 100],        # Whiskers extend to real min/max
        widths=0.6,
        showfliers=False,     # No Tukey outliers
        medianprops=dict(linewidth=1.2),
        boxprops=dict(linewidth=0.8),
        whiskerprops=dict(linewidth=0.8),
        capprops=dict(linewidth=0.8)
    )

    ax.set_xticklabels(LABELS)
    ax.set_ylabel("Miss Penalty (cycles)")
    ax.set_title("Miss Penalty Distribution")

    ax.grid(axis="y", linestyle="--", linewidth=0.5)

    plt.tight_layout()
    plt.savefig(OUTPUT_FILE)
    plt.close(fig)

    print(f"Saved figure to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()