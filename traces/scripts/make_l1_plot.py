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
    "../parsed/final/radix-4-l1-ctrl.csv",
    "../parsed/final/fmm-4-l1-ctrl.csv",
    "../parsed/final/fft-4-l1-ctrl.csv",
]   

LABELS = [
    "Radix-4",
    "FMM-4",
    "FFT-4",
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

    # Compute summary stats
    means = [np.mean(d) for d in all_data]
    max_vals = [np.max(d) for d in all_data]

    # Violin plot
    violins = ax.violinplot(
        all_data,
        showmeans=False,
        showmedians=False,
        showextrema=False,
        widths=0.7,
        points=500
    )

    # Style violins
    for body in violins['bodies']:
        body.set_alpha(0.7)

    # Plot mean bars
    for i, mean in enumerate(means, start=1):
        ax.hlines(
            mean,
            i - 0.25,
            i + 0.25,
            linewidth=1.4
        )

    # Plot absolute max markers
    ax.scatter(
        range(1, len(max_vals) + 1),
        max_vals,
        marker="_",
        s=400,
        linewidths=1.5
    )

    ax.set_xticks(range(1, len(LABELS) + 1))
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