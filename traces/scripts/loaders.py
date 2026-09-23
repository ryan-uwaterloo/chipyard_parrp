#!/usr/bin/env python3
"""
loaders.py
----------
All data-loading routines for plot_per_test.py.

Each public function returns a plain np.ndarray (or a dict of arrays for
reqtime).  Callers decide which arrays to plot.
"""

import os
import csv
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# Shared constants
# ============================================================

CHUNK_SIZE         = 5_000_000
DEFAULT_DATA_START = 50_000
ACQUIRE_OPCODES    = {"AcquireBlock", "AcquirePerm"}
RELEASE_OPCODES    = {"Release", "ReleaseData"}
CACHE_TYPES        = ["data", "inst"]

# Default core lists — override per-test via cfg["num_cores"]
CORES_4 = [0, 1, 2, 3]
CORES_8 = [0, 1, 2, 3, 4, 5, 6, 7]

def get_cores(num_cores: int = 4) -> list[int]:
    """Return the list of core indices for a given core count."""
    if num_cores == 4:
        return CORES_4
    if num_cores == 8:
        return CORES_8
    # Generic fallback for any count
    return list(range(num_cores))

# ============================================================
# Static SourceID → (core, cache_type) map  (from script 2)
# ============================================================

def _build_source_id_map() -> dict[int, tuple[int, str]]:
    """
    Map every integer SourceID that can appear in an LLC CSV to
    (core, cache_type).  Derived from TLMasterParameters.

      Core 0:  inst 48-53   data 56-62
      Core 1:  inst 32-38   data 40-46
      Core 2:  inst 16-22   data 24-30
      Core 3:  inst  0-6    data  8-14
    """
    ranges = [
        (0, 48, 53, "inst"), (0, 53, 54, "inst"),
        (0, 56, 61, "data"), (0, 61, 62, "data"),
        (1, 32, 37, "inst"), (1, 37, 38, "inst"),
        (1, 40, 45, "data"), (1, 45, 46, "data"),
        (2, 16, 21, "inst"), (2, 21, 22, "inst"),
        (2, 24, 29, "data"), (2, 29, 30, "data"),
        (3,  0,  5, "inst"), (3,  5,  6, "inst"),
        (3,  8, 13, "data"), (3, 13, 14, "data"),
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


# ============================================================
# Path helpers
# ============================================================

def build_paths(test: str, data_dir: str) -> dict[str, str]:
    """
    Return a dict of all CSV paths for a given test name.

    Keys
    ----
    l1_ctrl, l1_parrp         - L1 miss records
    rel_ctrl, rel_parrp       - L1 release records
    llc_ctrl, llc_parrp       - LLC completions
    probe_ctrl, probe_parrp   - probe latency records
    dram_ctrl, dram_parrp     - DRAM access records
    """
    d = data_dir
    return {
        "l1_ctrl":     f"{d}/{test}-l1-ctrl.csv",
        "l1_parrp":    f"{d}/{test}-l1-parrp-wb.csv",
        "rel_ctrl":    f"{d}/{test}-l1-ctrl_releases.csv",
        "rel_parrp":   f"{d}/{test}-l1-parrp-wb_releases.csv",
        "llc_ctrl":    f"{d}/{test}-ctrl.csv",
        "llc_parrp":   f"{d}/{test}-parrp-wb.csv",
        "probe_ctrl":  f"{d}/{test}-ctrl-probes.csv",
        "probe_parrp": f"{d}/{test}-parrp-wb-probes.csv",
        "dram_ctrl":   f"{d}/{test}-ctrl-dram.csv",
        "dram_parrp":  f"{d}/{test}-parrp-wb-dram.csv",
    }


def build_paths_iso_int(base: str, data_dir: str) -> dict[str, dict[str, str]]:
    """
    Return paths for an isolation/interference test pair.

    The filename convention is:
        inter-{iso|int}-{base}-{ctrl|parrp-wb}.csv   (and derivative suffixes)

    Parameters
    ----------
    base     : suffix after iso/int, e.g. "4"  -> inter-iso-4-ctrl.csv
    data_dir : directory containing the CSVs

    Returns
    -------
    {
      "iso": { same keys as build_paths() },
      "int": { same keys as build_paths() },
    }
    """
    return {
        condition: build_paths(f"inter-{condition}-{base}", data_dir)
        for condition in ("iso", "int")
    }

def build_paths_variants(test: str, variant_a: str, variant_b: str, data_dir: str) -> dict[str, str]:
    """
    Like build_paths(), but for two independently-named variants instead of
    the built-in ctrl/parrp-wb pair. Useful for ablations that compare two
    stock-side configs against each other (e.g. MSHR count sweeps).
    """
    d = data_dir
    return {
        "l1_a":     f"{d}/{test}-l1-{variant_a}.csv",
        "l1_b":     f"{d}/{test}-l1-{variant_b}.csv",
        "rel_a":    f"{d}/{test}-l1-{variant_a}_releases.csv",
        "rel_b":    f"{d}/{test}-l1-{variant_b}_releases.csv",
        "llc_a":    f"{d}/{test}-{variant_a}.csv",
        "llc_b":    f"{d}/{test}-{variant_b}.csv",
        "probe_a":  f"{d}/{test}-{variant_a}-probes.csv",
        "probe_b":  f"{d}/{test}-{variant_b}-probes.csv",
        "dram_a":   f"{d}/{test}-{variant_a}-dram.csv",
        "dram_b":   f"{d}/{test}-{variant_b}-dram.csv",
    }

def reqtime_csv_path(test_variant: str, core: int, cache_type: str,
                     memreq_dir: str) -> Path:
    return Path(memreq_dir) / f"{test_variant}.{core}_{cache_type}.csv"


# ============================================================
# Metric loaders
# ============================================================

def load_miss_penalty(filepath: str, data_start: int = DEFAULT_DATA_START) -> np.ndarray:
    """
    Load L1 miss-penalty values (uint16 cycles) filtered by `data_start`
    and a minimum penalty of 10 cycles.
    """
    chunks = []
    for chunk in pd.read_csv(
        filepath, usecols=["MissPenalty", "StartCycle"], chunksize=CHUNK_SIZE
    ):
        values      = chunk["MissPenalty"].to_numpy(dtype=np.uint16)
        start_cyc   = chunk["StartCycle"].to_numpy(dtype=np.uint64)
        mask        = (start_cyc >= data_start) & (values >= 10)
        filtered    = values[mask]
        if len(filtered):
            chunks.append(filtered)

    return np.concatenate(chunks) if chunks else np.array([], dtype=np.uint16)


# ------------------------------------------------------------------
# Eviction time  (two variants: ctrl uses "Latency" column directly;
#                 parrp requires join with L1 intervals)
# ------------------------------------------------------------------

def load_eviction_time_ctrl(release_filepath: str, data_start: int = DEFAULT_DATA_START) -> np.ndarray:
    """
    Stock (ctrl) eviction time: the 'Latency' column of the release CSV,
    filtered by `data_start`.
    """
    chunks = []
    for chunk in pd.read_csv(
        release_filepath, usecols=["Latency", "StartCycle"], chunksize=CHUNK_SIZE
    ):
        mask   = chunk["StartCycle"] >= data_start
        values = chunk.loc[mask, "Latency"].to_numpy(dtype=np.int32)
        if len(values):
            chunks.append(values)

    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int32)


def load_eviction_time_parrp(
    release_filepath: str,
    l1_filepath: str,
    num_sets: int = 64,
    data_start: int = DEFAULT_DATA_START,
    output_path: str | None = None,
) -> np.ndarray:
    """
    Modified (parrp) eviction time: computed by matching each release cycle
    to the L1 miss interval that contains it, then measuring the distance
    to the interval's end - 3 cycles.

    `num_sets` must be a power of 2 (64 or 128 depending on the test).

    If `output_path` is given (or left as the default derived from
    `release_filepath`), also writes a CSV with columns
    Address, Core, StartCycle, EvictionTime for every matched entry —
    written chunk-by-chunk so it's safe on very large traces.
    """
    assert (num_sets & (num_sets - 1)) == 0, \
        f"num_sets must be a power of 2, got {num_sets}"

    def index_bits(addr_series):
        ints = np.array([int(x, 16) for x in addr_series], dtype=np.int64)
        return ((ints >> 6) & (num_sets - 1)).astype(np.int32)

    if output_path is None:
        output_path = f"../parsed/parrp_evict/parrp-{os.path.basename(release_filepath)}"
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Load all L1 intervals once
    l1 = pd.read_csv(
        l1_filepath,
        usecols=["Address", "Core", "StartCycle", "EndCycle"],
    )
    l1["Core"]      = l1["Core"].str.lower()
    l1["IndexBits"] = index_bits(l1["Address"])
    l1["TargetCycle"] = (l1["EndCycle"] - 3).astype(np.int32)
    l1 = (
        l1[["Core", "IndexBits", "StartCycle", "EndCycle", "TargetCycle"]]
        .rename(columns={"StartCycle": "l1_start", "EndCycle": "l1_end"})
        .sort_values(["Core", "IndexBits", "l1_start"])
        .reset_index(drop=True)
    )

    results = []
    with open(output_path, "w", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow(["Address", "Core", "StartCycle", "EvictionTime"])

        for chunk in pd.read_csv(
            release_filepath,
            usecols=["Address", "Core", "StartCycle"],
            chunksize=CHUNK_SIZE,
        ):
            chunk = chunk[chunk["StartCycle"] >= data_start].copy()
            chunk["Core"]      = chunk["Core"].str.lower()
            chunk["IndexBits"] = index_bits(chunk["Address"])

            eviction_times = []
            out_rows = []
            for (core, idx_bits), rel_group in chunk.groupby(["Core", "IndexBits"]):
                l1_group = l1[(l1["Core"] == core) & (l1["IndexBits"] == idx_bits)]
                if l1_group.empty:
                    continue

                starts   = l1_group["l1_start"].to_numpy()
                ends     = l1_group["l1_end"].to_numpy()
                targets  = l1_group["TargetCycle"].to_numpy()
                rel_cyc  = rel_group["StartCycle"].to_numpy()
                rel_addr = rel_group["Address"].to_numpy()

                pos = np.searchsorted(starts, rel_cyc, side="right") - 1
                for cycle, addr, p in zip(rel_cyc, rel_addr, pos):
                    if p >= 0 and starts[p] <= cycle <= ends[p]:
                        ev_time = int(targets[p] - cycle)
                        eviction_times.append(ev_time)
                        out_rows.append((addr, core, int(cycle), ev_time))

            if out_rows:
                writer.writerows(out_rows)
            if eviction_times:
                results.append(np.array(eviction_times, dtype=np.int32))

    print(f"Eviction-time detail written to {output_path}")

    return np.concatenate(results) if results else np.array([], dtype=np.int32)


def load_llc_residual(llc_filepath: str, data_start: int = DEFAULT_DATA_START) -> np.ndarray:
    """
    SourceD-to-complete latency for LLC Release / ReleaseData opcodes,
    filtered by `data_start`.
    """
    chunks = []
    for chunk in pd.read_csv(
        llc_filepath,
        usecols=["Opcode", "SourceDToComplete", "StartCycle"],
        chunksize=CHUNK_SIZE,
    ):
        mask = (
            chunk["Opcode"].str.strip().isin(RELEASE_OPCODES)
            & (chunk["StartCycle"] >= data_start)
        )
        values = (
            pd.to_numeric(chunk.loc[mask, "SourceDToComplete"], errors="coerce")
            .dropna()
            .to_numpy(dtype=np.int32)
        )
        if len(values):
            chunks.append(values)

    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int32)


def load_probe_latency(probe_filepath: str, data_start: int = DEFAULT_DATA_START) -> np.ndarray:
    """
    Probe latency values, filtered to cycles >= `data_start`.
    """
    chunks = []
    for chunk in pd.read_csv(
        probe_filepath, usecols=["Latency", "StartCycle"], chunksize=CHUNK_SIZE
    ):
        mask   = chunk["StartCycle"] >= data_start
        values = (
            pd.to_numeric(chunk.loc[mask, "Latency"], errors="coerce")
            .dropna()
            .to_numpy(dtype=np.int32)
        )
        if len(values):
            chunks.append(values)

    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int32)


def load_dram(filepath: str, data_start: int = DEFAULT_DATA_START) -> np.ndarray:
    """
    DRAM access latency (uint16), filtered by `data_start`.
    """
    chunks = []
    for chunk in pd.read_csv(
        filepath, usecols=["StartCycle", "Latency"], chunksize=CHUNK_SIZE
    ):
        mask   = chunk["StartCycle"].to_numpy(dtype=np.uint64) >= data_start
        values = chunk["Latency"].to_numpy(dtype=np.uint16)[mask]
        if len(values):
            chunks.append(values)

    return np.concatenate(chunks) if chunks else np.array([], dtype=np.uint16)


def load_reqtimes(
    test_variant: str,
    cache_type: str,
    memreq_dir: str,
    cores: list[int] | None = None,          # ← new
) -> dict[str, np.ndarray]:
    """
    Aggregate request-time values across all cores for a test variant
    and cache_type ('data' | 'inst').

    Returns
    -------
    {"Load": np.ndarray, "Store": np.ndarray}
    """
    if cores is None:
        cores = CORES_4                       # preserve old default

    accum: dict[str, list[np.ndarray]] = {"Load": [], "Store": []}

    for core in cores:
        path = reqtime_csv_path(test_variant, core, cache_type, memreq_dir)
        if not path.exists() or os.path.getsize(path) == 0:
            print(f"  [WARN] missing/empty: {path}")
            continue
        print(f"  Reading {path.name} …")
        for chunk in pd.read_csv(
            path, usecols=["nodeType", "reqTime"], chunksize=CHUNK_SIZE
        ):
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
# Hit / miss summary  (kept for completeness; used by script 2 only)
# ============================================================

def _parse_source_id(raw) -> int | None:
    try:
        s = str(raw).strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (ValueError, TypeError):
        return None


def compute_hit_miss(
    test_name: str,
    variant: str,
    data_dir: str,
    memreq_dir: str,
) -> pd.DataFrame:
    """
    Return a DataFrame with columns:
      core, cache_type, misses, total_requests, hits, hit_rate
    """
    miss_counts: dict[tuple[int, str], int] = {}

    llc_path = Path(data_dir) / f"{test_name}-{variant}.csv"
    if llc_path.exists():
        llc_df = pd.read_csv(llc_path, usecols=["SourceID", "Opcode"])
        acquire_mask = llc_df["Opcode"].isin(ACQUIRE_OPCODES)
        for raw_sid in llc_df.loc[acquire_mask, "SourceID"]:
            sid = _parse_source_id(raw_sid)
            if sid is None:
                continue
            key = SOURCE_ID_MAP.get(sid)
            if key is None:
                print(f"  [warn] unmapped SourceID {raw_sid!r} (int={sid})")
                continue
            miss_counts[key] = miss_counts.get(key, 0) + 1
    else:
        print(f"  [warn] LLC CSV not found: {llc_path}")

    total_counts: dict[tuple[int, str], int] = {}
    for cache_type in CACHE_TYPES:
        for core in CORES:
            p = Path(memreq_dir) / f"{test_name}-{variant}.{core}_{cache_type}.csv"
            if not p.exists():
                continue
            n = sum(1 for _ in open(p)) - 1
            total_counts[(core, cache_type)] = max(0, n)

    all_keys = sorted(set(miss_counts) | set(total_counts))
    rows = []
    for (core, ctype) in all_keys:
        misses   = miss_counts.get((core, ctype), 0)
        total    = total_counts.get((core, ctype), 0)
        hits     = max(0, total - misses)
        hit_rate = (hits / total) if total > 0 else float("nan")
        rows.append({
            "core": core, "cache_type": ctype,
            "misses": misses, "total_requests": total,
            "hits": hits, "hit_rate": hit_rate,
        })

    return pd.DataFrame(
        rows,
        columns=["core", "cache_type", "misses", "total_requests", "hits", "hit_rate"],
    )

# ============================================================
# Address/Core normalization + state-join helpers
# ============================================================

FLAG_COLUMNS = ["NeedDRAM", "NeedProbe", "Evicting", "BackInv"]


def normalize_hex(series: pd.Series) -> pd.Series:
    """'0X40001C00', '0x3', '41' -> canonical int64. Handles mixed hex/decimal
    by always treating as hex since every Address/Core/SourceID field in these
    CSVs is written with an 0x/0X prefix."""
    return series.astype(str).str.strip().apply(lambda s: int(s, 16)).astype(np.int64)


def load_l1_miss_penalty_df(filepath: str, data_start: int = DEFAULT_DATA_START) -> pd.DataFrame:
    """Full L1 miss rows (not just the MissPenalty array) — needed as the left
    side of the state join."""
    df = pd.read_csv(
        filepath, usecols=["Address", "Core", "StartCycle", "EndCycle", "MissPenalty"]
    )
    df["MissPenalty"] = df["MissPenalty"].astype(np.uint16)
    mask = (df["StartCycle"] >= data_start) & (df["MissPenalty"] >= 10)
    return df.loc[mask].reset_index(drop=True)


def load_llc_full_df(filepath: str) -> pd.DataFrame:
    """Full LLC completion rows including the new Address column and state flags."""
    return pd.read_csv(
        filepath,
        usecols=["Address", "SourceID", "Opcode", "StartCycle", "EndCycle", "NodeType"] + FLAG_COLUMNS,
    )


def load_probe_df(filepath: str, data_start: int = DEFAULT_DATA_START) -> pd.DataFrame:
    df = pd.read_csv(filepath, usecols=["Set", "MSHRSource", "StartCycle", "EndCycle", "Latency"])
    return df.loc[df["StartCycle"] >= data_start].reset_index(drop=True)


def attach_state_miss_penalty(
    l1_df: pd.DataFrame, llc_df: pd.DataFrame, label: str = ""
) -> pd.DataFrame:
    """Join each L1 miss row to the nearest LLC transaction for the same
    (Address, Core) at or after the L1 request's StartCycle."""
    l1 = l1_df.copy()
    llc = llc_df.copy()

    l1["Address"] = normalize_hex(l1["Address"])
    l1["Core"] = normalize_hex(l1["Core"])
    llc["Address"] = normalize_hex(llc["Address"])
    llc["Core"] = normalize_hex(llc["SourceID"]).map(
        lambda sid: SOURCE_ID_MAP.get(sid, (None, None))[0]
    )
    llc = llc.dropna(subset=["Core"]).copy()
    llc["Core"] = llc["Core"].astype(np.int64)

    l1 = l1.sort_values("StartCycle").reset_index(drop=True)
    llc = llc.sort_values("StartCycle").reset_index(drop=True)

    merged = pd.merge_asof(
        l1, llc,
        on="StartCycle",
        by=["Address", "Core"],
        direction="forward",
        suffixes=("", "_llc"),
    )

    unmatched = merged["Opcode"].isna()
    n_unmatched = int(unmatched.sum())
    if n_unmatched:
        print(
            f"  [WARN] {label}: {n_unmatched}/{len(merged)} miss-penalty rows "
            f"had no matching LLC transaction (dropped)"
        )
    return merged.loc[~unmatched].reset_index(drop=True)


def attach_state_probe_latency(
    probe_df: pd.DataFrame, llc_df: pd.DataFrame, label: str = ""
) -> pd.DataFrame:
    """Join each probe row to the LLC transaction (matched by SourceID ==
    MSHRSource) whose cycle window contains the probe's StartCycle."""
    probes = probe_df.copy()
    llc = llc_df.copy()

    probes["SourceID"] = normalize_hex(probes["MSHRSource"])
    llc["SourceID"] = normalize_hex(llc["SourceID"])
    probes["Core"] = probes["SourceID"].map(
        lambda sid: SOURCE_ID_MAP.get(sid, (None, None))[0]
    )

    llc_sorted = llc.sort_values("StartCycle")
    out_rows = []
    n_unmatched = 0

    for sid, grp in probes.groupby("SourceID"):
        candidates = llc_sorted[llc_sorted["SourceID"] == sid]
        if candidates.empty:
            n_unmatched += len(grp)
            continue
        starts = candidates["StartCycle"].to_numpy()
        ends = candidates["EndCycle"].to_numpy()
        cand_records = candidates[["Opcode"] + FLAG_COLUMNS].to_dict("records")

        for _, prow in grp.iterrows():
            pos = np.searchsorted(starts, prow["StartCycle"], side="right") - 1
            if pos >= 0 and starts[pos] <= prow["StartCycle"] <= ends[pos]:
                out_rows.append({**prow.to_dict(), **cand_records[pos]})
            else:
                n_unmatched += 1

    if n_unmatched:
        print(
            f"  [WARN] {label}: {n_unmatched}/{len(probes)} probe rows had no "
            f"containing LLC transaction (dropped)"
        )
    return pd.DataFrame(out_rows)


def apply_facet_group(df: pd.DataFrame, group: dict) -> pd.DataFrame:
    """group = {"cores": [...], "flags": [...], "label": "..."}
    flags entries matching FLAG_COLUMNS are AND'd as boolean filters;
    anything else is matched against Opcode."""
    mask = df["Core"].isin(group["cores"])
    for flag in group["flags"]:
        if flag in FLAG_COLUMNS:
            mask &= df[flag].astype(bool)
        elif flag in ("Load", "Store"):
            mask &= (df["NodeType"] == flag)
        else:
            mask &= (df["Opcode"] == flag)
    return df.loc[mask]


def load_miss_penalty_faceted(
    l1_ctrl_path: str, l1_mod_path: str,
    llc_ctrl_path: str, llc_mod_path: str,
    facet_groups: list[dict],
    data_start: int = DEFAULT_DATA_START,
    label: str = "",
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    l1_ctrl = load_l1_miss_penalty_df(l1_ctrl_path, data_start)
    l1_mod = load_l1_miss_penalty_df(l1_mod_path, data_start)
    llc_ctrl = load_llc_full_df(llc_ctrl_path)
    llc_mod = load_llc_full_df(llc_mod_path)

    joined_ctrl = attach_state_miss_penalty(l1_ctrl, llc_ctrl, label=f"{label}-ctrl")
    joined_mod = attach_state_miss_penalty(l1_mod, llc_mod, label=f"{label}-mod")

    results = []
    for group in facet_groups:
        stock = apply_facet_group(joined_ctrl, group)["MissPenalty"].to_numpy(dtype=np.uint16)
        mod = apply_facet_group(joined_mod, group)["MissPenalty"].to_numpy(dtype=np.uint16)
        results.append((group["label"], stock, mod))
    return results


def load_probe_latency_faceted(
    probe_ctrl_path: str, probe_mod_path: str,
    llc_ctrl_path: str, llc_mod_path: str,
    facet_groups: list[dict],
    data_start: int = DEFAULT_DATA_START,
    label: str = "",
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    probe_ctrl = load_probe_df(probe_ctrl_path, data_start)
    probe_mod = load_probe_df(probe_mod_path, data_start)
    llc_ctrl = load_llc_full_df(llc_ctrl_path)
    llc_mod = load_llc_full_df(llc_mod_path)

    joined_ctrl = attach_state_probe_latency(probe_ctrl, llc_ctrl, label=f"{label}-ctrl")
    joined_mod = attach_state_probe_latency(probe_mod, llc_mod, label=f"{label}-mod")

    results = []
    for group in facet_groups:
        stock = apply_facet_group(joined_ctrl, group)["Latency"].to_numpy(dtype=np.int32)
        mod = apply_facet_group(joined_mod, group)["Latency"].to_numpy(dtype=np.int32)
        results.append((group["label"], stock, mod))
    return results