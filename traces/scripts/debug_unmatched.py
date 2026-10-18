#!/usr/bin/env python3
"""
debug_unmatched.py
------------------
Explains the "[WARN] ...: N/M miss-penalty rows had no matching LLC transaction"
message from loaders.attach_state_miss_penalty.

It repeats the same join (merge_asof, forward, by Address [+ Core]), then for
every L1 row that found no LLC transaction it works out WHY, using only the two
CSVs (no raw trace needed), and prints a category table plus a few examples
with a ready-made awk/grep command to look at the raw log around each one.

Usage
-----
    python debug_unmatched.py L1.csv LLC.csv [--by-core] [--data-start N]
                              [--show 3] [--tail-window 2000]
                              [--log trace.log] [--out unmatched.csv]

Match the join the warning came from:
  * join_by_core=True  (default in TEST_CONFIGS)  -> pass --by-core
  * join_by_core=False (e.g. probe-gen-*)         -> omit it
  * --data-start must equal the test's data_start (default 50000)

Example for the warning on probe-gen-4-ctrl:
    python debug_unmatched.py ../parsed/synthetics/probe-gen-4-l1-ctrl.csv \
                              ../parsed/synthetics/probe-gen-4-ctrl.csv
"""

import argparse

import numpy as np
import pandas as pd

import loaders as L


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("l1", help="L1 CSV (e.g. probe-gen-4-l1-ctrl.csv)")
    ap.add_argument("llc", help="LLC CSV (e.g. probe-gen-4-ctrl.csv)")
    ap.add_argument("--by-core", action="store_true",
                    help="join on (Address, Core), i.e. join_by_core=True")
    ap.add_argument("--data-start", type=int, default=L.DEFAULT_DATA_START)
    ap.add_argument("--tail-window", type=int, default=2000,
                    help="L1 rows starting within this many cycles of the last LLC "
                         "transaction count as 'tail of trace' (default 2000)")
    ap.add_argument("--show", type=int, default=3, help="examples per category")
    ap.add_argument("--log", default="TRACE.log",
                    help="raw trace path, only used in the printed grep hints")
    ap.add_argument("--out", help="write every unmatched row + diagnosis to this CSV")
    a = ap.parse_args()

    l1 = L.load_l1_miss_penalty_df(a.l1, a.data_start)
    llc = L.load_llc_full_df(a.llc)

    blank = llc["Address"].isna()
    if blank.any():
        print(f"[note] {int(blank.sum())} LLC rows have a blank Address (dropped)")
        llc = llc[~blank]

    l1["Address"] = L.normalize_hex(l1["Address"])
    l1["Core"] = L.normalize_hex(l1["Core"])
    llc["Address"] = L.normalize_hex(llc["Address"])
    llc["LlcCore"] = L.normalize_hex(llc["SourceID"]).map(
        lambda s: L.SOURCE_ID_MAP.get(s, (None, None))[0])
    any_llc = llc.sort_values("StartCycle").reset_index(drop=True)

    # ---- same join as attach_state_miss_penalty ----------------------------
    right, by = any_llc, ["Address"]
    if a.by_core:
        n_unmapped = int(right["LlcCore"].isna().sum())
        if n_unmapped:
            print(f"[note] {n_unmapped} LLC rows have a SourceID not in SOURCE_ID_MAP "
                  f"(cannot be matched with --by-core)")
        right = right.dropna(subset=["LlcCore"]).copy()
        right["Core"] = right["LlcCore"].astype(np.int64)
        by = ["Address", "Core"]
    right = right[by + ["StartCycle"]].assign(_hit=1)
    left = l1.sort_values("StartCycle").reset_index(drop=True)
    merged = pd.merge_asof(left, right, on="StartCycle", by=by, direction="forward")
    um = merged[merged["_hit"].isna()].drop(columns="_hit").copy()

    print(f"L1 rows (after data_start/penalty filters): {len(left)}")
    print(f"Unmatched: {len(um)}  ({len(um) / max(len(left), 1):.2%})   "
          f"join = {'Address+Core' if a.by_core else 'Address only'}")
    if um.empty:
        return

    # ---- diagnose each unmatched row ---------------------------------------
    trace_last = int(any_llc["StartCycle"].max())
    addrs = set(um["Address"])
    llc_by_addr = {ad: g for ad, g in any_llc[any_llc["Address"].isin(addrs)].groupby("Address")}
    l1_by_key = {k: g for k, g in left[left["Address"].isin(addrs)].groupby(["Address", "Core"])}

    cats, near_b, near_a = [], [], []
    for r in um.itertuples():
        g = llc_by_addr.get(r.Address)
        before = after_rows = None
        if g is not None:
            before_rows = g[g["StartCycle"] < r.StartCycle]
            after_rows = g[g["StartCycle"] >= r.StartCycle]
            before = before_rows.iloc[-1] if len(before_rows) else None
        sib = l1_by_key[(r.Address, r.Core)]
        inflight = ((sib["StartCycle"] < r.StartCycle) & (sib["EndCycle"] >= r.StartCycle)).any()
        has_after = after_rows is not None and len(after_rows) > 0

        if r.StartCycle >= trace_last - a.tail_window:
            cat = "tail_of_trace"
        elif inflight:
            cat = "overlaps_inflight_L1_miss_same_line"
        elif g is None:
            cat = "no_llc_row_for_address"
        elif not has_after:
            cat = "llc_rows_only_before_l1_start"
        elif a.by_core and after_rows["LlcCore"].isna().any() \
                and not (after_rows["LlcCore"] == r.Core).any():
            cat = "llc_rows_after_have_unmapped_source"
        elif a.by_core and not (after_rows["LlcCore"] == r.Core).any():
            cat = "llc_rows_after_but_other_core"
        else:
            cat = "unexplained"
        cats.append(cat)
        near_b.append(before)
        near_a.append(after_rows.iloc[0] if has_after else None)

    um["Category"] = cats
    um["PrevLlcStart"] = [b["StartCycle"] if b is not None else np.nan for b in near_b]
    um["PrevLlcEnd"] = [b["EndCycle"] if b is not None else np.nan for b in near_b]
    um["PrevLlcSource"] = [f"0x{int(b['SourceID'], 16):X}" if b is not None else "" for b in near_b]
    um["NextLlcStart"] = [x["StartCycle"] if x is not None else np.nan for x in near_a]
    um["NextLlcSource"] = [f"0x{int(x['SourceID'], 16):X}" if x is not None else "" for x in near_a]

    print(f"\nLast LLC transaction starts at cycle {trace_last}\n")
    print(um["Category"].value_counts().to_string())

    meaning = {
        "tail_of_trace": "L1 miss near the end of the run; its LLC transaction never completed "
                         "(or was never logged). Harmless.",
        "overlaps_inflight_L1_miss_same_line": "another L1 miss to the same line/core was still in "
                         "flight; likely a secondary miss merged into it (no new LLC request).",
        "no_llc_row_for_address": "no LLC transaction for this address at any time; the parser "
                         "dropped it (see 'Parser-side causes' below) or the LLC Address is wrong.",
        "llc_rows_only_before_l1_start": "LLC rows for this address exist, but all started before "
                         "the L1 request; check PrevLlcStart/End to see if one covers it.",
        "llc_rows_after_have_unmapped_source": "a later LLC row exists but its SourceID is not in "
                         "SOURCE_ID_MAP.",
        "llc_rows_after_but_other_core": "a later LLC row for the address exists, but from a "
                         "different core (core/SourceID mapping or --by-core mismatch).",
        "unexplained": "a later matching LLC row exists, so the join should have matched; "
                       "tell me about these.",
    }
    for cat in um["Category"].unique():
        print(f"\n--- {cat}: {meaning[cat]}")
        for r in um[um["Category"] == cat].head(a.show).itertuples():
            lo, hi = r.StartCycle - 20, r.EndCycle + 20
            print(f"  addr=0x{r.Address:08X} core={r.Core} L1 {r.StartCycle}->{r.EndCycle} "
                  f"(pen={r.MissPenalty}) | prev LLC {r.PrevLlcStart}->{r.PrevLlcEnd} "
                  f"src={r.PrevLlcSource or '-'} | next LLC {r.NextLlcStart} src={r.NextLlcSource or '-'}")
            print(f"    awk '$3+0>={lo} && $3+0<={hi}' {a.log} | grep -i '0x{r.Address:08x}'")

    print("\nParser-side causes to check when category is no_llc_row_for_address:")
    print("  * parse_log.py COMPLETION_COOLDOWN=4: a Sink A/C arrival for the same (source, opcode)")
    print("    within 4 cycles of that source's last completion is treated as a tail beat and ignored,")
    print("    so a back-to-back reuse of a source ID loses its start and its row is never emitted.")
    print("  * Acquire completions are rejected when no Source D was seen since the sink arrived.")
    print("  * 'Completion unmatched' / 'Duplicate completion suppressed' in --debug output.")
    print("  Run parse_log.py --debug and grep it for the source ID printed in the LLC rows nearby.")

    if a.out:
        um.to_csv(a.out, index=False)
        print(f"\nAll unmatched rows → {a.out}")


if __name__ == "__main__":
    main()