#!/usr/bin/env python3
"""
stats_per_test.py
-----------------
Text summary (n / min / mean / median / p99 / p99.9 / max) of every slot that
plot_per_test.py would draw, for both TEST_CONFIGS and ABLATION_CONFIGS.

It reuses TEST_CONFIGS / ABLATION_CONFIGS / METRIC_DEFS / DIRECTORIES from
plot_per_test.py, so toggling a metric or editing facet_by there changes
this report too.

Usage
-----
    python stats_per_test.py                       # everything, to stdout
    python stats_per_test.py --only hol-4 probe-4  # filter by cfg["test"]
    python stats_per_test.py --out stats.txt --csv stats.csv
    python stats_per_test.py --no-ablation
"""

import argparse
import csv

import numpy as np

import plot_per_test as ppt
from loaders import (
    DEFAULT_DATA_START,
    build_paths,
    build_paths_variants,
    load_miss_penalty_faceted,
    load_probe_latency_faceted,
)


# ============================================================
# Slot collection (mirrors plot_one_test / plot_ablation_test)
# ============================================================

def collect_slots(cfg: dict, dirs: dict, ablation: bool):
    """Return [(slot_title, arr_first, arr_second), ...] in plot order."""
    test       = cfg["test"]
    facet_by   = cfg.get("facet_by", {})
    data_start = cfg.get("data_start", DEFAULT_DATA_START)
    order      = cfg.get("order", ppt.DEFAULT_METRIC_ORDER)
    active     = [m for m in order if cfg["metrics"].get(m, False)]

    if ablation:
        va, vb = cfg["variant_a"], cfg["variant_b"]
        paths  = build_paths_variants(test, va, vb, dirs["data"])
        mdefs  = ppt.ABLATION_METRIC_DEFS
        k1, k2 = ("l1_a", "l1_b"), ("llc_a", "llc_b")
        kp     = ("probe_a", "probe_b")
    else:
        paths  = build_paths(test, dirs["data"])
        mdefs  = ppt.METRIC_DEFS
        k1, k2 = ("l1_ctrl", "l1_parrp"), ("llc_ctrl", "llc_parrp")
        kp     = ("probe_ctrl", "probe_parrp")

    slots = []
    for metric in active:
        if metric in facet_by:
            groups = facet_by[metric]
            if metric == "miss_penalty":
                slots.extend(load_miss_penalty_faceted(
                    paths[k1[0]], paths[k1[1]], paths[k2[0]], paths[k2[1]],
                    groups, data_start=data_start, label=test))
            elif metric == "probe_latency":
                slots.extend(load_probe_latency_faceted(
                    paths[kp[0]], paths[kp[1]], paths[k2[0]], paths[k2[1]],
                    groups, data_start=data_start, label=test))
            else:
                raise NotImplementedError(f"facet_by not wired up for '{metric}'")
        else:
            mdef = mdefs[metric]
            a, b = mdef["loader"](
                paths, {**cfg, "test": test, "data_start": data_start}, dirs)
            slots.append((mdef["title"], a, b))
    return slots


# ============================================================
# Statistics
# ============================================================

STAT_KEYS = ["n", "min", "mean", "median", "p99", "p99.9", "max"]


def summarize(arr: np.ndarray) -> dict:
    if arr is None or len(arr) == 0:
        return {k: None for k in STAT_KEYS} | {"n": 0}
    a = np.asarray(arr, dtype=np.float64)   # avoid uint16 overflow surprises
    return {
        "n":      int(a.size),
        "min":    float(a.min()),
        "mean":   float(a.mean()),
        "median": float(np.median(a)),
        "p99":    float(np.percentile(a, 99)),
        "p99.9":  float(np.percentile(a, 99.9)),
        "max":    float(a.max()),
    }


def fmt(v, width=11, prec=1):
    if v is None:
        return f"{'-':>{width}}"
    return f"{v:>{width},.{prec}f}"


def pct(new, old):
    if new is None or old in (None, 0):
        return "-"
    return f"{(new - old) / old * 100:+.1f}%"


# ============================================================
# Report
# ============================================================

def report_config(cfg, dirs, ablation, out_lines, csv_rows):
    if ablation:
        names = (cfg["variant_a"], cfg["variant_b"])
        names = cfg.get("legend_labels", names)
        header = f"ABLATION: {cfg['label']}  [{cfg['test']}: {names[0]} vs {names[1]}]"
    else:
        names = ("Stock", "Parrp")
        header = f"TEST: {cfg['label']}  [{cfg['test']}]"

    slots = collect_slots(cfg, dirs, ablation)

    out_lines.append("=" * 100)
    out_lines.append(header)
    out_lines.append("=" * 100)
    out_lines.append(
        f"{'Metric':<24}{'Variant':<22}{'N':>10}"
        + "".join(f"{k:>11}" for k in STAT_KEYS[1:])
    )
    out_lines.append("-" * 100)

    for title, first, second in slots:
        flat = title.replace("\n", " ")
        s1, s2 = summarize(first), summarize(second)
        for name, s in zip(names, (s1, s2)):
            out_lines.append(
                f"{flat:<24}{name:<22}{s['n']:>10,}"
                + "".join(fmt(s[k]) for k in STAT_KEYS[1:])
            )
            csv_rows.append({
                "kind": "ablation" if ablation else "test",
                "test": cfg["test"], "label": cfg["label"],
                "metric": flat, "variant": name, **s,
            })
        out_lines.append(
            f"{'':<24}{'Δ (2nd vs 1st)':<22}{'':>10}"
            f"{'':>11}{pct(s2['mean'], s1['mean']):>11}"
            f"{pct(s2['median'], s1['median']):>11}"
            f"{pct(s2['p99'], s1['p99']):>11}"
            f"{pct(s2['p99.9'], s1['p99.9']):>11}"
            f"{pct(s2['max'], s1['max']):>11}"
        )
        out_lines.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="only cfg['test'] names in this list")
    ap.add_argument("--no-ablation", action="store_true")
    ap.add_argument("--out", help="also write the text report to this file")
    ap.add_argument("--csv", help="also write a flat CSV of all stats")
    args = ap.parse_args()

    def keep(cfg):
        return not args.only or cfg["test"] in args.only

    lines, rows = [], []
    for cfg in filter(keep, ppt.TEST_CONFIGS):
        report_config(cfg, ppt.DIRECTORIES, False, lines, rows)
    if not args.no_ablation:
        for cfg in filter(keep, ppt.ABLATION_CONFIGS):
            report_config(cfg, ppt.DIRECTORIES, True, lines, rows)

    text = "\n".join(lines)
    print("\n" + text)

    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"Text report → {args.out}")
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=["kind", "test", "label", "metric", "variant"] + STAT_KEYS)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV → {args.csv}")


if __name__ == "__main__":
    main()