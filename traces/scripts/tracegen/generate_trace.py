#!/usr/bin/env python3
"""
generate_trace.py

Parametric synthetic memory-trace generator for RTL cache-hierarchy simulation.

Produces a tab-separated CSV matching the schema:
    seq_num  type  p_addr  size  flags  rob_dep  comp_delay  reg_dep  weight  pc  v_addr  asid

Two phases, both optional and independently configurable:

  1. PRIMING phase
     Deliberately fills N "ways" (distinct tags) of M "sets" (distinct set-index
     offsets) so the cache reaches a known, full state before the randomized
     test vector runs. Ops within a (set, way-fill) group are chained via
     reg_dep to preserve issue order; each set's group starts a fresh chain.

  2. RANDOM phase
     Randomly selects addresses (from the primed address pool, or from a wider
     region) and issues LOAD/STORE per configured probabilities. The first
     `parallel_start_width` ops of this phase get `priming_delay` on comp_delay
     (this is what creates the gap between priming finishing and the random
     vector actually starting) and no reg_dep (independent chain heads).
     Remaining ops round-robin-chain onto those heads via reg_dep, `width`
     streams deep, similar to your hand-written example (161-164 -> 165-168 -> ...).

     Each of the `width` parallel chains is pinned to its own disjoint subset
     of active_sets for its entire lifetime (see `sets_per_chain`) -- this
     mirrors your hand-written trace, where chain heads 161-164 stay locked to
     sets 12/13/14/15 respectively. Two chains never touch the same set, so
     they can't queue behind each other in the hierarchy's per-set ordering
     and defeat the purpose of running them concurrently.

Usage:
    python3 generate_trace.py --config config.yaml --out trace.csv
    python3 generate_trace.py --out trace.csv          # uses built-in defaults

Config file can be YAML or JSON (detected by extension). See config.yaml
for a fully-commented example matching your snippet's geometry.
"""

import argparse
import csv
import json
import random
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

CSV_COLUMNS = [
    "seq_num", "type", "p_addr", "size", "flags", "rob_dep",
    "comp_delay", "reg_dep", "weight", "pc", "v_addr", "asid",
]

DEFAULT_CONFIG = {
    "seed": 42,
    "base_addr": 0x40000000,
    "cache": {
        "line_size_bytes": 64,      # bits [5:0] offset
        "num_sets_total": 64,       # full cache; drives tag_stride = num_sets_total * line_size
                                     # (one "tag page" holds every set exactly once)
    },
    # Which set indices (0 .. num_sets_total-1) this trace is allowed to touch.
    # Either give an explicit list, or a contiguous partition (e.g. core N's slice).
    "active_sets": {
        "mode": "partition",        # "partition" -> contiguous slice; "explicit" -> use `sets` list
        "sets_per_partition": 16,
        "core_id": 0,
        "sets": None,                # used when mode == "explicit", e.g. [0,1,2,...,15]
    },
    "priming": {
        "enabled": True,
        "associativity": 10,        # distinct tags primed per set
        "op_type": "STORE",         # "STORE", "LOAD", or "random"
        "load_prob": 0.5,           # used only if op_type == "random"
        "comp_delay": 100,
        "comp_delay_jitter": 0,     # +/- uniform random jitter added to comp_delay per op
        "chain_dependencies": True,
    },
    "random_phase": {
        "enabled": True,
        "num_ops": 200,
        "addr_pool": "primed",     # "primed" -> reuse addresses touched during priming (exact tag,set pairs)
                                    # "region"  -> random tag x random set within the chain's assigned sets
        "tag_range": 64,            # number of distinct tag values to sample from when addr_pool == "region"
        "load_prob": 0.5,
        "comp_delay": 100,
        "comp_delay_jitter": 20,
        "priming_delay": 50_000_000,     # comp_delay applied to the first `parallel_start_width` ops
        "priming_delay_jitter": 0,       # +/- uniform random jitter added to priming_delay
        "parallel_start_width": 4,       # number of independent chains kicked off after the priming delay
        "sets_per_chain": None,          # None -> split active_sets as evenly as possible across the
                                          # `parallel_start_width` chains (disjoint, no overlap). Set to an
                                          # int (e.g. 1) to force each chain onto exactly that many sets,
                                          # matching the hand-written example's one-set-per-chain pattern.
        "shuffle_set_partition": True,   # shuffle active_sets before splitting, so chain->set assignment
                                          # isn't always the same for a given active_sets list
        "chain_dependencies": True,
        "size": 64,
    },
}


def partition_active_sets(active_sets, width, sets_per_chain, rng, shuffle=True):
    """
    Split active_sets into `width` disjoint groups, one per parallel chain,
    so no two concurrently in-flight chains ever target the same set (which
    would serialize behind the hierarchy's per-set queuing and defeat the
    point of running them in parallel).
    """
    pool = list(active_sets)
    if shuffle:
        rng.shuffle(pool)

    if sets_per_chain is not None:
        needed = width * sets_per_chain
        if needed > len(pool):
            sys.exit(
                f"random_phase: parallel_start_width={width} * sets_per_chain={sets_per_chain} "
                f"= {needed} sets requested, but only {len(pool)} active sets are available"
            )
        return [pool[i * sets_per_chain:(i + 1) * sets_per_chain] for i in range(width)]

    if width > len(pool):
        sys.exit(
            f"random_phase: parallel_start_width={width} exceeds the number of active sets "
            f"({len(pool)}); can't give each chain a disjoint set. Reduce parallel_start_width, "
            f"grow active_sets, or set sets_per_chain explicitly."
        )

    # split as evenly as possible: some chains get one extra set if it doesn't divide evenly
    groups = [[] for _ in range(width)]
    for i, s in enumerate(pool):
        groups[i % width].append(s)
    return groups


def resolve_active_sets(cfg):
    acfg = cfg["active_sets"]
    total = cfg["cache"]["num_sets_total"]
    if acfg["mode"] == "explicit":
        sets = acfg["sets"]
        if not sets:
            sys.exit("active_sets.mode == 'explicit' requires a non-empty 'sets' list")
        return list(sets)
    # partition mode: contiguous slice for this core
    n = acfg["sets_per_partition"]
    core = acfg["core_id"]
    start = core * n
    if start + n > total:
        sys.exit(f"partition for core_id={core} (sets {start}..{start+n-1}) exceeds num_sets_total={total}")
    return list(range(start, start + n))


def jittered(rng, base, jitter):
    if jitter <= 0:
        return base
    return max(1, base + rng.randint(-jitter, jitter))


def load_config(path):
    if path is None:
        return DEFAULT_CONFIG
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        if yaml is None:
            sys.exit("pyyaml not installed; install it or pass a .json config")
        user_cfg = yaml.safe_load(text)
    else:
        user_cfg = json.loads(text)
    return deep_merge(DEFAULT_CONFIG, user_cfg or {})


def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def new_row(seq_num, op_type, p_addr, size, comp_delay, reg_dep=None):
    return {
        "seq_num": seq_num,
        "type": op_type,
        "p_addr": f"0x{p_addr:08X}",
        "size": size,
        "flags": "",
        "rob_dep": "",
        "comp_delay": comp_delay,
        "reg_dep": reg_dep if reg_dep is not None else "",
        "weight": "",
        "pc": "",
        "v_addr": "",
        "asid": "",
    }


def pick_type(rng, op_type_cfg, load_prob):
    if op_type_cfg == "random":
        return "LOAD" if rng.random() < load_prob else "STORE"
    return op_type_cfg


def generate_priming(cfg, rng, seq_start, active_sets, tag_stride, line_size):
    """
    Fill `associativity` distinct tags for each active set index.
    Returns (rows, next_seq, addr_pool) where addr_pool is the list of
    (set_idx, addr) pairs actually touched.
    """
    pcfg = cfg["priming"]
    rows = []
    addr_pool = []
    seq = seq_start

    for set_idx in active_sets:
        prev_seq = None
        for way_idx in range(pcfg["associativity"]):
            addr = cfg["base_addr"] + way_idx * tag_stride + set_idx * line_size
            addr_pool.append((set_idx, addr))
            op_type = pick_type(rng, pcfg["op_type"], pcfg["load_prob"])
            reg_dep = prev_seq if (pcfg["chain_dependencies"] and prev_seq is not None) else None
            comp_delay = jittered(rng, pcfg["comp_delay"], pcfg["comp_delay_jitter"])
            rows.append(new_row(seq, op_type, addr, line_size, comp_delay, reg_dep))
            prev_seq = seq
            seq += 1

    return rows, seq, addr_pool


def generate_random_phase(cfg, rng, seq_start, addr_pool, active_sets, tag_stride, line_size):
    rcfg = cfg["random_phase"]
    rows = []
    seq = seq_start
    width = max(1, rcfg["parallel_start_width"])

    chain_set_groups = partition_active_sets(
        active_sets, width, rcfg["sets_per_chain"], rng, rcfg["shuffle_set_partition"]
    )
    # Pre-split the primed addr_pool by chain, so "primed" mode also respects
    # the disjoint-set-per-chain requirement.
    primed_by_chain = []
    for group in chain_set_groups:
        group_set = set(group)
        primed_by_chain.append([addr for (s, addr) in addr_pool if s in group_set])

    def pick_addr(stream):
        if rcfg["addr_pool"] == "primed" and primed_by_chain[stream]:
            return rng.choice(primed_by_chain[stream])
        # "region": locked to this chain's assigned sets, tag chosen from a
        # wider range than what was primed -> exercises unseen tags while
        # never touching a set another concurrent chain owns.
        set_idx = rng.choice(chain_set_groups[stream])
        tag_idx = rng.randrange(0, rcfg["tag_range"])
        return cfg["base_addr"] + tag_idx * tag_stride + set_idx * line_size

    chain_heads = []  # seq_num of the most recent op in each of `width` parallel streams
    for i in range(rcfg["num_ops"]):
        stream = i % width if i >= width else i
        addr = pick_addr(stream)
        op_type = "LOAD" if rng.random() < rcfg["load_prob"] else "STORE"

        if i < width:
            # independent chain head: gets the priming delay, no reg_dep
            comp_delay = jittered(rng, rcfg["priming_delay"], rcfg["priming_delay_jitter"])
            reg_dep = None
            row = new_row(seq, op_type, addr, rcfg["size"], comp_delay, reg_dep)
            chain_heads.append(seq)
        else:
            reg_dep = chain_heads[stream] if rcfg["chain_dependencies"] else None
            comp_delay = jittered(rng, rcfg["comp_delay"], rcfg["comp_delay_jitter"])
            row = new_row(seq, op_type, addr, rcfg["size"], comp_delay, reg_dep)
            chain_heads[stream] = seq

        rows.append(row)
        seq += 1

    return rows, seq


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="Path to YAML or JSON config file")
    ap.add_argument("--out", default="trace.csv", help="Output CSV path")
    args = ap.parse_args()

    cfg = load_config(args.config)
    rng = random.Random(cfg["seed"])

    active_sets = resolve_active_sets(cfg)
    line_size = cfg["cache"]["line_size_bytes"]
    tag_stride = cfg["cache"].get("tag_stride") or cfg["cache"]["num_sets_total"] * line_size

    rows = []
    seq = 1
    addr_pool = []

    if cfg["priming"]["enabled"]:
        prime_rows, seq, addr_pool = generate_priming(cfg, rng, seq, active_sets, tag_stride, line_size)
        rows.extend(prime_rows)

    if cfg["random_phase"]["enabled"]:
        rand_rows, seq = generate_random_phase(cfg, rng, seq, addr_pool, active_sets, tag_stride, line_size)
        rows.extend(rand_rows)

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} ops to {args.out} (seed={cfg['seed']})")


if __name__ == "__main__":
    main()
