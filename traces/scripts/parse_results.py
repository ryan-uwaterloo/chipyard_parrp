#!/usr/bin/env python3
import re
import argparse
import csv
import os
from collections import defaultdict

def addr_set(addr):
    """Extract set bits [11:6] from an address."""
    return (addr >> 6) & 0x3F

def parse_log(filepath, csv_out=None, l1_out=None, debug=False):
    # Regex patterns
    sink_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Sink ([ACX]) Request! opcode:\s*(\w+).*source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    # Updated: now captures set and tag fields from the new log format
    meta_re = re.compile(
        r"@ clk_cycle\s+(\d+): Req in MSHR; need dram\?: (\d+), need probe\? (\d+), evicting\? (\d+), back-inv\? (\d+), source: (0x[0-9a-fA-F]+), set: (0x[0-9a-fA-F]+), tag: (0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    stall_re = re.compile(
        r"@ clk_cycle\s+(\d+): ReleaseData prevented from entering SinkC due to no putbuff space!",
        re.IGNORECASE)
    complete_re = re.compile(
        r"@ clk_cycle\s+(\d+):\s*(\w+)\s+Request completed; sent to directory!\s*source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    source_d_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Source D Request! opcode:\s*(\w+), source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    source_a_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Source A Request! opcode:\s*(\w+), addr:\s*(0x[0-9a-fA-F]+), source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    sink_d_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Sink D Request! opcode:\s*(\w+), source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    # L1 Regexes
    l1_new_re = re.compile(
        r"@ clk_cycle\s+(\d+): New L1 Request! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    l1_data_re = re.compile(
        r"@ clk_cycle\s+(\d+): L1 Request data sent to core! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    l1_free_re = re.compile(
        r"@ clk_cycle\s+(\d+): L1 Request MSHR Free! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    l1_release_new_re = re.compile(
        r"@ clk_cycle\s+(\d+): New L1 Release! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    l1_release_complete_re = re.compile(
        r"@ clk_cycle\s+(\d+): L1 Release Complete! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    source_b_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Source B Request! opcode: ProbeBlock, param: \d+, addr:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    sink_c_probeack_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Sink C Request! opcode: ProbeAck(?:Data)?, addr:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)


    # --- State maps ---
    sink_times = {}       # (source, opcode) -> start_cycle
    request_meta = {}     # source -> metadata dict
    last_completion = {}  # (source, opcode) -> last completion cycle
    last_source_d = {}    # (source, source_d_opcode) -> last Source D cycle seen
    results = []          # [(source, opcode, start, end, latency, metadata, stalls, source_d_cycle)]
    l1_start = {}         # (addr, core) -> cycle
    l1_data = {}          # (addr, core) -> cycle
    l1_results = []       # results list
    l1_release_start = {}   # (addr, core) -> cycle
    l1_release_results = [] # (addr, core, start, end, latency)

    # Probe tracking keyed by *set* (bits [11:6] of the address).
    #
    # Multiple MSHR entries can target the same set concurrently, so we keep a
    # list of (start_cycle, mshr_source) per set.  When a ProbeAck arrives, its
    # address is also reduced to the set, and we match it against the oldest
    # pending entry for that set once all expected acks have arrived.
    #
    # probe_start[set]       – list of (start_cycle, mshr_source), FIFO order
    # l1_probe_expected[set] – total acks still expected for the *current* probe
    # l1_probe_received[set] – acks received so far for the current probe
    #
    # When received >= expected the front entry is popped and counters reset,
    # ready for the next probe on the same set.
    probe_start = defaultdict(list)      # set -> [(start_cycle, mshr_source), ...]
    l1_probe_expected = defaultdict(int) # set -> expected ack count
    l1_probe_received = defaultdict(int) # set -> received ack count
    probe_results = []                   # (set, start, end, latency, mshr_source)

    pending_source_d = {}  # src -> (opcode, start_cycle, end_cycle, latency, meta, stall_cycles)
    source_a_times = {}   # (src, opcode) -> start_cycle
    dram_results = []     # [(src, start, end, latency)]
    l1_hits = defaultdict(int)  # core -> hit count

    # Mapping from completion opcode to the expected Source D opcode
    # Release/ReleaseData are acknowledged by ReleaseAck;
    # Acquire-family opcodes are fulfilled by Grant or GrantData.
    COMPLETION_TO_SOURCE_D_OPCODE = {
        "Release":     ["ReleaseAck"],
        "ReleaseData": ["ReleaseAck"],
        "AcquireBlock": ["GrantData", "Grant"],
        "AcquirePerm":  ["Grant"],
    }
    RELEASE_OPCODES = {"Release", "ReleaseData"}

    # --- Stall tracking ---
    release_stalls = defaultdict(lambda: {"last_cycle": None, "count": 0})

    # --- Stats ---
    total_sink_c = 0
    ignored_sink_c = 0
    accepted_sink_c = 0

    DUPLICATE_COMPLETION_WINDOW = 2
    COMPLETION_COOLDOWN = 4

    with open(filepath, "r") as f:
        for line_no, line in enumerate(f, 1):

            # ---------------------------------------------------------------
            # ProbeAck (Sink C): match by *set* of the ack address.
            # The ack address is not guaranteed to equal the MSHR address, but
            # bits [11:6] (the set) must agree.
            # ---------------------------------------------------------------
            if m := sink_c_probeack_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                s     = addr_set(addr)

                if probe_start[s]:
                    l1_probe_received[s] += 1
                    if debug:
                        print(f"[line {line_no}] PROBE ACK addr=0x{addr:X} (set=0x{s:X}), "
                              f"received={l1_probe_received[s]}, expected={l1_probe_expected[s]}")
                    if l1_probe_received[s] >= l1_probe_expected[s]:
                        # All acks received — complete the oldest pending probe.
                        start_cycle, mshr_source = probe_start[s].pop(0)
                        latency = cycle - start_cycle
                        probe_results.append((s, start_cycle, cycle, latency, mshr_source))
                        if debug:
                            print(f"[line {line_no}] PROBE COMPLETE set=0x{s:X}, "
                                  f"start={start_cycle}, end={cycle}, lat={latency}, "
                                  f"mshr_source=0x{mshr_source:X}")
                        # Reset per-set counters; the next probe on this set
                        # will increment l1_probe_expected again from the MSHR line.
                        l1_probe_expected[s] = 0
                        l1_probe_received[s] = 0
                else:
                    if debug:
                        print(f"[line {line_no}] PROBE ACK addr=0x{addr:X} (set=0x{s:X}) "
                              f"— no pending probe for this set, ignoring")
                continue

            # --- Match Sink arrivals ---
            if m := sink_re.search(line):
                cycle = int(m[1])
                sink_type = m[2].upper()
                opcode = m[3]
                src = int(m[4], 16)
                sink_key = (src, opcode)

                last = last_completion.get(sink_key)
                if last is not None and (cycle - last) <= COMPLETION_COOLDOWN:
                    if debug:
                        print(f"[line {line_no}] Ignoring tail beat after completion for {sink_key}")
                    continue

                # Sink C filtering
                if sink_type == "C":
                    total_sink_c += 1
                    if opcode not in ("Release", "ReleaseData"):
                        ignored_sink_c += 1
                        if debug:
                            print(f"[line {line_no}] Ignored Sink C ({opcode}) source=0x{src:X}")
                        continue
                    accepted_sink_c += 1

                # Record only first valid arrival for this (src, opcode)
                if sink_key not in sink_times:
                    sink_times[sink_key] = cycle
                    if debug:
                        print(f"[line {line_no}] Sink {sink_type}: cycle={cycle}, opcode={opcode}, source=0x{src:X}")
                else:
                    if debug:
                        print(f"[line {line_no}] Duplicate Sink {sink_type} for source=0x{src:X}, opcode={opcode}")
                continue

            # ---------------------------------------------------------------
            # MSHR metadata line — now also carries set and tag.
            # If need_probe is set, probe timing starts HERE (not at Source B).
            # ---------------------------------------------------------------
            if m := meta_re.search(line):
                cycle      = int(m[1])
                dram       = int(m[2])
                need_probe = int(m[3])
                evicting   = int(m[4])
                back_inv   = int(m[5])
                src        = int(m[6], 16)
                mshr_set   = int(m[7], 16)   # bits [11:6] directly from the log
                tag        = int(m[8], 16)

                # Look up the sink arrival time for this source so we can later
                # compute SetBlockTime = MSHR_entry_cycle - sink_arrival_cycle.
                # We search sink_times for any opcode matching this source because
                # the MSHR line doesn't carry the opcode directly.
                candidates = [t for (s, _op), t in sink_times.items() if s == src and t <= cycle]
                sink_start = max(candidates) if candidates else None

                request_meta[src] = {
                    "need_dram":  dram,
                    "need_probe": need_probe,
                    "evicting":   evicting,
                    "back_inv":   back_inv,
                    "meta_cycle": cycle,
                    "set":        mshr_set,
                    "tag":        tag,
                    "sink_start": sink_start,  # None if MSHR somehow precedes sink (shouldn't happen)
                }

                if need_probe:
                    # Start probe timing from this MSHR arrival cycle.
                    probe_start[mshr_set].append((cycle, src))
                    l1_probe_expected[mshr_set] += 1
                    if debug:
                        print(f"[line {line_no}] PROBE START (MSHR) set=0x{mshr_set:X}, "
                              f"source=0x{src:X}, cycle={cycle}, "
                              f"total_expected_now={l1_probe_expected[mshr_set]}")

                if debug:
                    print(f"[line {line_no}] Metadata captured for source=0x{src:X}: {request_meta[src]}")
                continue

            # --- Match ReleaseData stalls ---
            if m := stall_re.search(line):
                cycle = int(m[1])
                release_stalls["pending"]["last_cycle"] = cycle
                release_stalls["pending"]["count"] += 1
                if debug:
                    print(f"[line {line_no}] ReleaseData stall at cycle={cycle} "
                          f"(count={release_stalls['pending']['count']})")
                continue

            # --- Match Source D ---
            if m := source_d_re.search(line):
                cycle    = int(m[1])
                sd_opcode = m[2]
                src      = int(m[3], 16)
                last_source_d[(src, sd_opcode)] = cycle

                # Resolve a deferred Release/ReleaseData completion
                if src in pending_source_d:
                    p_opcode, p_start, p_end, p_latency, p_meta, p_stalls = pending_source_d[src]
                    expected_sd = COMPLETION_TO_SOURCE_D_OPCODE.get(p_opcode, [])
                    if sd_opcode in expected_sd:
                        results.append((src, p_opcode, p_start, p_end, p_latency, p_meta, p_stalls, cycle))
                        del pending_source_d[src]
                        if debug:
                            print(f"[line {line_no}] Resolved deferred Release: source=0x{src:X}, "
                                  f"source_d={cycle}, d_to_complete={p_end - cycle}")
                continue

            # --- Match completions ---
            if m := complete_re.search(line):
                cycle  = int(m[1])
                opcode = m[2]
                src    = int(m[3], 16)
                sink_key = (src, opcode)

                # Skip duplicate completions within short window
                last = last_completion.get(sink_key)
                if last is not None and (cycle - last) < DUPLICATE_COMPLETION_WINDOW:
                    if debug:
                        print(f"[line {line_no}] Duplicate completion suppressed for {sink_key}, "
                              f"cycle={cycle}, last={last}")
                    last_completion[sink_key] = cycle
                    continue

                # Must have a matching sink
                if sink_key not in sink_times:
                    if debug:
                        print(f"[line {line_no}] Completion unmatched: source=0x{src:X}, "
                              f"opcode={opcode}, cycle={cycle}")
                    last_completion[sink_key] = cycle
                    continue

                start_cycle = sink_times[sink_key]

                # Attach pending stall info for ReleaseData
                stall_cycles = 0
                if opcode == "ReleaseData":
                    stall_info = release_stalls.pop("pending", None)
                    stall_cycles = stall_info["count"] if stall_info else 0

                latency = cycle - start_cycle
                meta = request_meta.get(src, {})

                if opcode in RELEASE_OPCODES:
                    # Source D (ReleaseAck) may arrive before or after completion.
                    # Check if it already arrived and is valid (after sink started).
                    sd_opcodes = COMPLETION_TO_SOURCE_D_OPCODE.get(opcode, [])
                    source_d_cycle = None
                    source_d_found_opcode = None
                    for sd_opcode in sd_opcodes:
                        val = last_source_d.get((src, sd_opcode))
                        if val is not None and val >= start_cycle:
                            source_d_cycle = val
                            source_d_found_opcode = sd_opcode
                            break

                    if source_d_cycle is not None:
                        # Source D already seen — commit immediately
                        last_source_d.pop((src, source_d_found_opcode), None)
                        results.append((src, opcode, start_cycle, cycle, latency, meta, stall_cycles, source_d_cycle))
                        if debug:
                            print(f"[line {line_no}] Completed (Source D already seen): "
                                  f"source=0x{src:X}, opcode={opcode}, "
                                  f"start={start_cycle}, end={cycle}, latency={latency}, "
                                  f"source_d={source_d_cycle}")
                    else:
                        # Source D not yet seen — defer until ReleaseAck arrives
                        pending_source_d[src] = (opcode, start_cycle, cycle, latency, meta, stall_cycles)
                        if debug:
                            print(f"[line {line_no}] Deferred Release completion for "
                                  f"source=0x{src:X}, awaiting ReleaseAck")

                    del sink_times[sink_key]
                    last_completion[sink_key] = cycle

                else:
                    # Acquire* path
                    sd_opcodes = COMPLETION_TO_SOURCE_D_OPCODE.get(opcode, [])
                    source_d_cycle = None
                    source_d_found_opcode = None
                    for sd_opcode in sd_opcodes:
                        val = last_source_d.get((src, sd_opcode))
                        if val is not None:
                            source_d_cycle = val
                            source_d_found_opcode = sd_opcode
                            break
                    else:
                        source_d_cycle = next(
                            (last_source_d[k] for k in last_source_d if k[0] == src), None
                        )

                    # Guard: reject if no valid Source D has been seen since the sink arrived
                    if source_d_cycle is None or source_d_cycle < start_cycle:
                        if debug:
                            print(f"[line {line_no}] Rejecting completion for {sink_key}: "
                                  f"no valid Source D (source_d={source_d_cycle}, start={start_cycle})")
                        last_completion[sink_key] = cycle
                        continue

                    # Commit: pop Source D and sink entry
                    if source_d_found_opcode is not None:
                        last_source_d.pop((src, source_d_found_opcode), None)
                    else:
                        for key in list(last_source_d):
                            if key[0] == src:
                                last_source_d.pop(key)
                                break

                    results.append((src, opcode, start_cycle, cycle, latency, meta, stall_cycles, source_d_cycle))

                    if debug:
                        source_d_to_complete = cycle - source_d_cycle
                        print(f"[line {line_no}] Completed: source=0x{src:X}, opcode={opcode}, "
                              f"start={start_cycle}, end={cycle}, latency={latency}, "
                              f"stalls={stall_cycles}, source_d={source_d_cycle}, "
                              f"source_d_to_complete={source_d_to_complete}, metadata={meta}")

                    del sink_times[sink_key]
                    last_completion[sink_key] = cycle

            # --- L1 new requests ---
            if m := l1_new_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)
                if key not in l1_start:
                    l1_start[key] = cycle
                    if debug:
                        print(f"[line {line_no}] L1 NEW addr=0x{addr:X}, core=0x{core:X}, cycle={cycle}")
                continue

            # --- L1 data to core ---
            if m := l1_data_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)
                l1_data[key] = cycle
                if debug:
                    print(f"[line {line_no}] L1 DATA addr=0x{addr:X}, cycle={cycle}")
                continue

            if m := l1_free_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)
                if key in l1_start:
                    start    = l1_start[key]
                    latency  = cycle - start
                    data_cycle = l1_data.get(key)
                    l1_results.append((addr, core, start, data_cycle, cycle, latency))
                    if debug:
                        print(f"[line {line_no}] L1 COMPLETE addr=0x{addr:X}, "
                              f"start={start}, data={data_cycle}, end={cycle}, lat={latency}")
                    del l1_start[key]
                    l1_data.pop(key, None)
                else:
                    if debug:
                        print(f"[line {line_no}] L1 completion unmatched addr=0x{addr:X}")
                continue

            if m := l1_release_new_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)
                if key not in l1_release_start:
                    l1_release_start[key] = cycle
                    if debug:
                        print(f"[line {line_no}] L1 RELEASE NEW addr=0x{addr:X}, core=0x{core:X}, cycle={cycle}")
                continue

            if m := l1_release_complete_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)
                if key in l1_release_start:
                    start   = l1_release_start[key]
                    latency = cycle - start
                    l1_release_results.append((addr, core, start, cycle, latency))
                    if debug:
                        print(f"[line {line_no}] L1 RELEASE COMPLETE addr=0x{addr:X}, "
                              f"start={start}, end={cycle}, lat={latency}")
                    del l1_release_start[key]
                else:
                    if debug:
                        print(f"[line {line_no}] L1 Release completion unmatched addr=0x{addr:X}")
                continue

            # Source B: no longer drives probe timing — MSHR arrival does.
            # Keep the regex match just for debug visibility.
            if m := source_b_re.search(line):
                if debug:
                    cycle = int(m[1])
                    addr  = int(m[2], 16)
                    print(f"[line {line_no}] Source B (ProbeBlock) addr=0x{addr:X}, cycle={cycle} "
                          f"— probe timing is driven by MSHR, not Source B")
                continue

            # Memory timing
            if m := source_a_re.search(line):
                cycle  = int(m[1])
                opcode = m[2]
                src    = int(m[4], 16)
                if opcode == "AcquireBlock":
                    key = (src, opcode)
                    if key not in source_a_times:
                        source_a_times[key] = cycle
                        if debug:
                            print(f"[line {line_no}] Source A: cycle={cycle}, opcode={opcode}, source=0x{src:X}")
                continue

            if m := sink_d_re.search(line):
                cycle  = int(m[1])
                opcode = m[2]
                src    = int(m[3], 16)
                if opcode == "GrantData":
                    key = (src, "AcquireBlock")
                    if key in source_a_times:
                        start   = source_a_times.pop(key)
                        latency = cycle - start
                        dram_results.append((src, start, cycle, latency))
                        if debug:
                            print(f"[line {line_no}] DRAM complete: source=0x{src:X}, "
                                  f"start={start}, end={cycle}, latency={latency}")
                continue

    # --- CSV output ---
    if csv_out:
        with open(csv_out, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "Opcode", "StartCycle", "EndCycle", "Latency",
                             "SetBlockTime",
                             "ReleaseStallCycles", "SourceDCycle", "SourceDToComplete",
                             "NeedDRAM", "NeedProbe", "Evicting", "BackInv"])
            for src, opcode, start, end, lat, meta, stalls, source_d_cycle in results:
                d_to_complete = (end - source_d_cycle) if source_d_cycle is not None else ""
                meta_cycle  = meta.get('meta_cycle')
                sink_start  = meta.get('sink_start')
                set_block_time = (meta_cycle - sink_start
                                  if meta_cycle is not None and sink_start is not None
                                  else "")
                writer.writerow([
                    f"0x{src:X}", opcode, start, end, lat,
                    set_block_time,
                    stalls,
                    source_d_cycle if source_d_cycle is not None else "",
                    d_to_complete,
                    meta.get('need_dram',''), meta.get('need_probe',''),
                    meta.get('evicting',''), meta.get('back_inv',''),
                ])
        print(f"\n Results with metadata written to {csv_out}")

        dram_csv = csv_out.replace(".csv", "-dram.csv")
        with open(dram_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "StartCycle", "EndCycle", "Latency"])
            for src, start, end, lat in dram_results:
                writer.writerow([f"0x{src:X}", start, end, lat])
        print(f"\n DRAM results written to {dram_csv}")

    # --- Summary stats ---
    print(f"\nSummary:")
    print(f"  {len(results)} completions matched")
    print(f"  {len(sink_times)} sinks still pending")
    print(f"  Sink C total: {total_sink_c} | accepted: {accepted_sink_c} | ignored: {ignored_sink_c}")

    # --- L1 CSV output ---
    if l1_out:
        os.makedirs(os.path.dirname(l1_out), exist_ok=True)

        with open(l1_out, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Address","Core","StartCycle","DataCycle","EndCycle","Latency","MissPenalty"])
            for addr, core, start, data, end, lat in l1_results:
                miss_penalty = (data - start if data is not None else "")
                writer.writerow([
                    f"{addr:#X}", f"0x{core:X}", start,
                    data if data is not None else "",
                    end, lat, miss_penalty
                ])
        print(f"\nL1 results written to {l1_out}")

    print(f"\nL1 Summary:")
    print(f"  {len(l1_results)} L1 requests completed")
    print(f"  {len(l1_start)} L1 requests still pending")

    if l1_out:
        release_csv = l1_out.replace(".csv", "_releases.csv")
        with open(release_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Address", "Core", "StartCycle", "EndCycle", "Latency"])
            for addr, core, start, end, lat in l1_release_results:
                writer.writerow([f"{addr:#X}", f"0x{core:X}", start, end, lat])
        print(f"\nL1 Release results written to {release_csv}")

        print(f"\nL1 Release Summary:")
        print(f"  {len(l1_release_results)} L1 releases completed")
        print(f"  {len(l1_release_start)} L1 releases still pending")

        # Probe CSV now uses Set as the key instead of Address
        probe_csv = csv_out.replace(".csv", "-probes.csv")
        with open(probe_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Set", "MSHRSource", "StartCycle", "EndCycle", "Latency"])
            for s, start, end, lat, mshr_source in probe_results:
                writer.writerow([f"0x{s:X}", f"0x{mshr_source:X}", start, end, lat])
        print(f"\nProbe results written to {probe_csv}")

        pending_probe_count = sum(len(v) for v in probe_start.values())
        print(f"\nL1 Probe Summary:")
        print(f"  {len(probe_results)} Probes completed")
        print(f"  {pending_probe_count} Probes still pending")

    # Optional: pending warnings in debug mode
    if debug and sink_times:
        print("\n [WARN] Pending sinks (unmatched requests):")
        for (src, opcode), start_cycle in sorted(sink_times.items(), key=lambda kv: kv[1]):
            print(f"    - Source 0x{src:X}, opcode={opcode}, issued @ {start_cycle}")

    if debug:
        pending_probe_count = sum(len(v) for v in probe_start.values())
        if pending_probe_count:
            print("\n [WARN] Pending probes (unmatched):")
            for s, entries in sorted(probe_start.items()):
                for start_cycle, mshr_source in entries:
                    print(f"    - Set=0x{s:X}, mshr_source=0x{mshr_source:X}, started @ {start_cycle}")

    # --- Min/Max latency summaries ---
    print("\nMin/Max Latency Events:")

    # LLC requests (results)
    if results:
        by_lat = sorted(results, key=lambda r: r[4])
        for label, r in [("LLC min", by_lat[0]), ("LLC max", by_lat[-1])]:
            src, opcode, start, end, lat, meta, stalls, source_d_cycle = r
            print(f"  {label}: opcode={opcode}, source=0x{src:X}, start={start}, end={end}, latency={lat}")

    # L1 requests
    if l1_results:
        by_lat = sorted(l1_results, key=lambda r: r[5])
        for label, r in [("L1 min", by_lat[0]), ("L1 max", by_lat[-1])]:
            addr, core, start, data, end, lat = r
            print(f"  {label}: addr=0x{addr:X}, core=0x{core:X}, start={start}, end={end}, latency={lat}")

    # L1 releases
    if l1_release_results:
        by_lat = sorted(l1_release_results, key=lambda r: r[4])
        for label, r in [("L1 release min", by_lat[0]), ("L1 release max", by_lat[-1])]:
            addr, core, start, end, lat = r
            print(f"  {label}: addr=0x{addr:X}, core=0x{core:X}, start={start}, end={end}, latency={lat}")

    # Probes
    if probe_results:
        by_lat = sorted(probe_results, key=lambda r: r[3])
        for label, r in [("Probe min", by_lat[0]), ("Probe max", by_lat[-1])]:
            s, start, end, lat, mshr_source = r
            print(f"  {label}: set=0x{s:X}, mshr_source=0x{mshr_source:X}, "
                  f"start={start}, end={end}, latency={lat}")

    # Max residual: largest |EndCycle - SourceDCycle| across all Release/ReleaseData results
    release_results_with_sd = [
        r for r in results
        if r[1] in RELEASE_OPCODES and r[7] is not None
    ]
    if release_results_with_sd:
        max_residual_r = max(release_results_with_sd, key=lambda r: abs(r[3] - r[7]))
        src, opcode, start, end, lat, meta, stalls, source_d_cycle = max_residual_r
        residual = abs(end - source_d_cycle)
        print(f"\nMax Source D Residual (Release/ReleaseData):")
        print(f"  residual={residual}, opcode={opcode}, source=0x{src:X}, "
              f"end={end}, source_d={source_d_cycle}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Compute request processing latency using (source, opcode) keys, "
                    "including ReleaseData stall tracking and metadata."
    )
    parser.add_argument("logfile", help="Path to the log file")
    parser.add_argument("--csv",   help="Optional output CSV file path")
    parser.add_argument("--l1csv", help="Optional output L1 CSV file path")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug printing")
    args = parser.parse_args()

    if not os.path.isfile(args.logfile):
        print(f"Error: file '{args.logfile}' not found.")
        return

    parse_log(args.logfile, csv_out=args.csv, l1_out=args.l1csv, debug=args.debug)


if __name__ == "__main__":
    main()