#!/usr/bin/env python3

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scienceplots
from matplotlib.patches import Patch

plt.style.use(['science', 'ieee'])

# ============================================================
# Configuration (Hard-coded for paper reproducibility)
# ============================================================

CSV_FILES_A = [
    "../parsed/final/radix-4-l1-ctrl.csv",
    "../parsed/final/fft-4-l1-ctrl.csv",
    "../parsed/final/fmm-4-l1-ctrl.csv",
]

CSV_FILES_B = [
    "../parsed/final/radix-4-l1-parrp.csv",
    "../parsed/final/fft-4-l1-parrp.csv",
    "../parsed/final/fmm-4-l1-parrp.csv",
]

LABELS = [
    "Radix-4",
    "FFT-4",
    "FMM-4",
]

OUTPUT_FILE = "miss_penalty_violin_joined_ieee.pdf"

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
        values = values[values >= 10] # trim secondary misses

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

    data_a = []
    mins_a = []
    maxs_a = []

    data_b = []
    mins_b = []
    maxs_b = []

    print("Loading CSV files...")

    for file in CSV_FILES_A:
        print(f"  Processing {file} ...")
        values, mn, mx = load_miss_penalty(file)
        data_a.append(values)
        mins_a.append(mn)
        maxs_a.append(mx)

        print("CONTROL")
        print(f"    Min: {mn}")
        print(f"    Max: {mx}")
        print(f"    Samples: {len(values)}")

    for file in CSV_FILES_B:
        print(f"  Processing {file} ...")
        values, mn, mx = load_miss_penalty(file)
        data_b.append(values)
        mins_b.append(mn)
        maxs_b.append(mx)

        print("PARRP")
        print(f"    Min: {mn}")
        print(f"    Max: {mx}")
        print(f"    Samples: {len(values)}")
        

    print("Generating plot...")

    gap = 0.01   # small horizontal offset

    fig, ax = plt.subplots()

    x = np.arange(1, len(LABELS) + 1)

    pos_a = x - gap
    pos_b = x + gap

    violins_a = ax.violinplot(
        data_a,
        positions=pos_a,
        widths=0.8,
        showmeans=False,
        showmedians=False,
        showextrema=False,
        points=500,
    )

    violins_b = ax.violinplot(
        data_b,
        positions=pos_b,
        widths=0.8,
        showmeans=False,
        showmedians=False,
        showextrema=False,
        points=500,
    )

    for i, body in enumerate(violins_a["bodies"]):
        path = body.get_paths()[0]
        verts = path.vertices
        verts[:,0] = np.minimum(verts[:,0], x[i] - gap)

    for i, body in enumerate(violins_b["bodies"]):
        path = body.get_paths()[0]
        verts = path.vertices
        verts[:,0] = np.maximum(verts[:,0], x[i] + gap)

    # Style violins
    for body in violins_a["bodies"]:
        body.set_facecolor("white")
        body.set_edgecolor("red")
        body.set_hatch("////////")
        body.set_alpha(1)

    for body in violins_b["bodies"]:
        body.set_facecolor("white")
        body.set_edgecolor("blue")
        body.set_hatch("xxxxxxxx")
        body.set_alpha(1)

    half_width = 0.32

    med_a = [np.median(d) for d in data_a]
    med_b = [np.median(d) for d in data_b]

    mean_a = [np.mean(d) for d in data_a]
    mean_b = [np.mean(d) for d in data_b]

    for i in range(len(x)):
        # mean lines
        ax.hlines(mean_a[i], x[i] - half_width, x[i] - gap, linewidth=0.8, color='k')
        ax.hlines(mean_b[i], x[i] + gap, x[i] + half_width, linewidth=0.8, color='r')


    # # Plot mean bars
    # for i, mean in enumerate(means, start=1):
    #     ax.hlines(
    #         mean,
    #         i - 0.25,
    #         i + 0.25,
    #         linewidth=1.4
    #     )

    # # Plot absolute max markers
    # ax.scatter(
    #     range(1, len(max_vals) + 1),
    #     max_vals,
    #     marker="_",
    #     s=400,
    #     linewidths=1.5
    # )

    ax.set_xticks(x)
    ax.set_xticklabels(LABELS)

    ax.set_ylabel("Miss Penalty (cycles)")
    ax.set_title("Miss Penalty Distribution")

    ax.grid(axis="y", linestyle="--", linewidth=0.5)

    legend_elements = [
        Patch(facecolor="white", edgecolor="red", hatch="////////", label="Stock"),
        Patch(facecolor="white", edgecolor="blue", hatch="xxxxxxxx", label="Parrp"),
    ]

    ax.legend(handles=legend_elements)

    plt.tight_layout()
    plt.rcParams["hatch.linewidth"] = 0.2

    plt.savefig(OUTPUT_FILE)
    plt.close(fig)

    print(f"Saved figure to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()