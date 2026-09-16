#!/usr/bin/env python3
"""
generate_multicore_trace.py

Multi-core extension of generate_trace.py. Generates one CSV per core from a
single coordinated config, so that a subset of addresses can be shared across
cores -- letting true-sharing, contended-synchronization-variable, and
migratory-sharing workloads fall out of the same mechanism (see the `shared`
config section). Reuses generate_trace.py's single-core building blocks
(priming, per-chain disjoint-set partitioning, jitter) for each core's private
address space; independent single-core traces from generate_trace.py are
unaffected and still work standalone for test cases that don't need sharing.

Design (no cross-core sync primitive assumed -- the harness runs every core's
trace freely, so this generator controls *where and how often* cores can
collide, not a guaranteed interleaving order):

  - `cache.num_sets_total` sets are split into:
      * a SHARED region (`shared.num_sets` sets, taken off the top) that
        every core's random phase may draw from
      * a PRIVATE region, sliced contiguously across cores' own
        `sets_per_partition` (exactly as in the single-core generator)

  - Each core's random phase, per op, rolls `shared_access_prob`: with that
    probability the op targets a random address from the shared pool
    (any set, any tag in `shared.tag_range` -- deliberately NOT restricted to
    that chain's disjoint set group, since cross-core/cross-chain collision
    on the shared pool is the point); otherwise it behaves exactly like the
    single-core generator's "region" mode, locked to that chain's own
    disjoint private sets.

  - `shared_load_prob` (per core) lets you assign asymmetric roles on the
    shared pool -- e.g. core 0 mostly stores (producer), cores 1-3 mostly
    load (consumers), or all cores near-symmetric for a contended
    read-modify-write variable. Same mechanism covers true sharing,
    contended-sync-variable, and migratory sharing -- they differ only in
    `shared.tag_range` (1 = single hot address; a handful = migratory-ish;
    larger = diffuse true sharing) and each core's shared_load_prob.

  - If `shared.prime` is set, one designated core (`shared.priming_owner`)
    additionally primes the shared pool at the start of its own trace
    (reusing generate_trace.generate_priming against the shared sets). Since
    there's no cross-core sync, this only establishes a "likely" initial
    state timing-wise, not a guaranteed happens-before -- which is fine for
    free-running harnesses; RTL sim + varied seeds explores the actual races.

Usage:
    python3 generate_multicore_trace.py --config multicore_config.yaml

Config file can be YAML or JSON. See multicore_config.yaml for a fully
commented example.
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

from generate_trace import (
    CSV_COLUMNS,
    new_row,
    jittered,
    deep_merge,
    generate_priming,
    partition_active_sets,
)

DEFAULT_CORE = {
    "sets_per_partition": 12,
    "priming": {
        "enabled": True,
        "associativity": 10,
        "op_type": "random",
        "load_prob": 0.5,
        "comp_delay": 100,
        "comp_delay_jitter": 15,
        "chain_dependencies": True,
    },
    "random_phase": {
        "enabled": True,
        "num_ops": 1000,
        "addr_pool": "primed",
        "tag_range": 64,
        "load_prob": 0.5,
        "comp_delay": 5000,
        "comp_delay_jitter": 20000,
        "priming_delay": 50_000_000,
        "priming_delay_jitter": 5000,
        "parallel_start_width": 4,
        "sets_per_chain": None,
        "shuffle_set_partition": True,
        "chain_dependencies": True,
        "size": 64,
        # multi-core additions:
        "shared_access_prob": 0.8,   # fraction of this core's random ops that target the shared pool
        "shared_load_prob": None,    # None -> fall back to this core's random_phase.load_prob
    },
}

DEFAULT_TOP = {
    "seed": 42,
    "base_addr": 0x40000000,
    "cache": {
        "line_size_bytes": 64,
        "num_sets_total": 64,
    },
    "shared": {
        "num_sets": 16,            # sets reserved for the shared pool (taken off the top, not private to any core)
        "tag_range": 8,             # distinct shared addresses per shared set (pool size = num_sets * tag_range)
        "prime": True,
        "prime_from_all_cores": False,  # False -> only `priming_owner` primes the shared pool;
                                          # True -> every core primes the full shared pool at the
                                          # start of its own trace (priming_owner is ignored)
        "priming_owner": 0,          # index into `cores` that primes the shared pool when prime_from_all_cores is False
        "priming_op_type": "STORE",
        "priming_comp_delay": 100,
        "priming_comp_delay_jitter": 15,
    },
    "cores": [],   # list of per-core configs, each merged onto DEFAULT_CORE
    "output": {
        "pattern": "trace_core{core_id}.csv",
        "delimiter": ",",
    },
}


def load_config(path):
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        if yaml is None:
            sys.exit("pyyaml not installed; install it or pass a .json config")
        user_cfg = yaml.safe_load(text)
    else:
        user_cfg = json.loads(text)
    return deep_merge(DEFAULT_TOP, user_cfg or {})


def build_shared_pool(shared_sets, tag_range, base_addr, tag_stride, line_size):
    """(set_idx, addr) pairs available to every core's shared-pool accesses."""
    pool = []
    for set_idx in shared_sets:
        for tag_idx in range(tag_range):
            addr = base_addr + tag_idx * tag_stride + set_idx * line_size
            pool.append((set_idx, addr))
    return pool


def resolve_private_sets(num_sets_total, shared_num_sets, cores):
    """
    Private pool = every set not reserved for sharing, sliced contiguously
    across cores in the order given, each taking its own `sets_per_partition`.
    """
    private_pool = list(range(num_sets_total - shared_num_sets))
    shared_sets = list(range(num_sets_total - shared_num_sets, num_sets_total))

    per_core_sets = []
    cursor = 0
    for i, core_cfg in enumerate(cores):
        n = core_cfg["sets_per_partition"]
        if cursor + n > len(private_pool):
            sys.exit(
                f"core {i}: private set allocation overflows available private sets "
                f"({cursor}..{cursor+n-1} requested, only {len(private_pool)} private sets exist "
                f"after reserving {shared_num_sets} for sharing). Reduce sets_per_partition or shared.num_sets."
            )
        per_core_sets.append(private_pool[cursor:cursor + n])
        cursor += n

    return per_core_sets, shared_sets


def generate_random_phase_shared(core_cfg, rng, seq_start, private_addr_pool, private_sets,
                                  shared_pool, tag_stride, line_size, base_addr):
    rcfg = core_cfg["random_phase"]
    rows = []
    seq = seq_start
    width = max(1, rcfg["parallel_start_width"])
    shared_prob = rcfg["shared_access_prob"]
    shared_load_prob = rcfg["shared_load_prob"]
    if shared_load_prob is None:
        shared_load_prob = rcfg["load_prob"]

    chain_set_groups = partition_active_sets(
        private_sets, width, rcfg["sets_per_chain"], rng, rcfg["shuffle_set_partition"]
    )
    primed_by_chain = []
    for group in chain_set_groups:
        group_set = set(group)
        primed_by_chain.append([addr for (s, addr) in private_addr_pool if s in group_set])

    def pick_private(stream):
        if rcfg["addr_pool"] == "primed" and primed_by_chain[stream]:
            return rng.choice(primed_by_chain[stream])
        set_idx = rng.choice(chain_set_groups[stream])
        tag_idx = rng.randrange(0, rcfg["tag_range"])
        return base_addr + tag_idx * tag_stride + set_idx * line_size

    def pick_shared():
        # Deliberately NOT restricted to the chain's disjoint set group --
        # cross-core/cross-chain collision here is the point.
        _, addr = rng.choice(shared_pool)
        return addr

    chain_heads = []
    for i in range(rcfg["num_ops"]):
        stream = i % width if i >= width else i
        is_shared = shared_prob > 0 and shared_pool and rng.random() < shared_prob

        if is_shared:
            addr = pick_shared()
            op_type = "LOAD" if rng.random() < shared_load_prob else "STORE"
        else:
            addr = pick_private(stream)
            op_type = "LOAD" if rng.random() < rcfg["load_prob"] else "STORE"

        if i < width:
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


def write_csv(path, rows, delimiter):
    with open(path, "w", newline="") as f:
        d = "\t" if delimiter == "\\t" else delimiter
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=d)
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="Path to YAML or JSON multi-core config file")
    args = ap.parse_args()

    top = load_config(args.config)
    if not top["cores"]:
        sys.exit("config must define at least one entry under `cores`")

    cores = [deep_merge(DEFAULT_CORE, c) for c in top["cores"]]
    for i, c in enumerate(cores):
        c.setdefault("core_id", i)

    line_size = top["cache"]["line_size_bytes"]
    num_sets_total = top["cache"]["num_sets_total"]
    tag_stride = top["cache"].get("tag_stride") or num_sets_total * line_size
    base_addr = top["base_addr"]

    scfg = top["shared"]
    per_core_private_sets, shared_sets = resolve_private_sets(num_sets_total, scfg["num_sets"], cores)

    # Address pool is deterministic (just set/tag geometry) -- build it once,
    # independent of which core(s) end up priming it, so pick_shared() always
    # has the full pool regardless of the priming config below.
    shared_pool = build_shared_pool(shared_sets, scfg["tag_range"], base_addr, tag_stride, line_size)

    shared_priming_cfg = {
        "base_addr": base_addr,
        "priming": {
            "associativity": scfg["tag_range"],
            "op_type": scfg["priming_op_type"],
            "load_prob": 0.5,
            "comp_delay": scfg["priming_comp_delay"],
            "comp_delay_jitter": scfg["priming_comp_delay_jitter"],
            "chain_dependencies": True,
        },
    }

    for i, core_cfg in enumerate(cores):
        core_id = core_cfg["core_id"]
        core_cfg["base_addr"] = base_addr
        core_rng = random.Random(f"{top['seed']}-{core_id}")
        private_sets = per_core_private_sets[i]

        rows = []
        seq = 1

        should_prime_shared = scfg["prime"] and (
            scfg["prime_from_all_cores"] or core_id == scfg["priming_owner"]
        )
        if should_prime_shared:
            # Each core primes with its own rng, so jitter (and op_type, if
            # "random") varies per core even though the address set is the
            # same shared pool every time.
            shared_prime_rows, seq, _ = generate_priming(
                shared_priming_cfg, core_rng, seq, shared_sets, tag_stride, line_size
            )
            rows.extend(shared_prime_rows)

        private_addr_pool = []
        if core_cfg["priming"]["enabled"]:
            prime_rows, seq, private_addr_pool = generate_priming(
                core_cfg, core_rng, seq, private_sets, tag_stride, line_size
            )
            rows.extend(prime_rows)

        if core_cfg["random_phase"]["enabled"]:
            rand_rows, seq = generate_random_phase_shared(
                core_cfg, core_rng, seq, private_addr_pool, private_sets,
                shared_pool, tag_stride, line_size, base_addr
            )
            rows.extend(rand_rows)

        out_path = top["output"]["pattern"].format(core_id=core_id)
        write_csv(out_path, rows, top["output"]["delimiter"])
        print(f"core {core_id}: wrote {len(rows)} ops to {out_path} "
              f"(private sets {private_sets[0]}-{private_sets[-1]}, shared sets {shared_sets[0]}-{shared_sets[-1]})")


if __name__ == "__main__":
    main()
