#!/usr/bin/env python3
import re
import argparse
import csv
import os
from collections import defaultdict, deque

def addr_set(addr):
    """Extract set bits [11:6] from an address."""
    return (addr >> 6) & 0x3F

def addr_tag(addr):
    """Extract the tag from an address, assuming set bits are [11:6]."""
    return addr >> 12

def parse_log(filepath, csv_out=None, l1_out=None, debug=False):
    # Regex patterns
    sink_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Sink ([ACX]) Request! opcode:\s*(\w+)"
        r"(?:.*?addr:\s*(0x[0-9a-fA-F]+))?.*source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    # Updated: now captures set and tag fields from the new log format
    meta_re = re.compile(
        r"@ clk_cycle\s+(\d+): Req in MSHR; need dram\?: (\d+), need probe\? (\d+), evicting\? (\d+), "
        r"back-inv\? (\d+), source: (0x[0-9a-fA-F]+), set: (0x[0-9a-fA-F]+), tag: (0x[0-9a-fA-F]+), "
        r"param: (0x[0-9a-fA-F]+)",
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
    # NEW: Source C (LLC -> memory writes). Only the opcode is parsed here; the
    # source/addr fields are pulled out of the remainder of the line separately
    # so the regex doesn't depend on the order of the fields you add to the log.
    source_c_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Source C Request! opcode:\s*(\w+)(.*)",
        re.IGNORECASE)
    source_field_re = re.compile(r"source:\s*(0x[0-9a-fA-F]+)", re.IGNORECASE)
    addr_field_re   = re.compile(r"addr:\s*(0x[0-9a-fA-F]+)", re.IGNORECASE)
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
    l1_release_in_re = re.compile(
        r"@ clk_cycle\s+(\d+): L1 Release In RelU! Address:\s*(0x[0-9a-fA-F]+), Core:\s*(0x[0-9a-fA-F]+)",
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
    l1_probe_new_re = re.compile(
        r"@ clk_cycle\s+(\d+): New L1 Probe Req! "
        r"Address:\s*(0x[0-9a-fA-F]+), "
        r"Core:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)


    # --- State maps ---
    sink_times = {}       # (source, opcode) -> start_cycle
    sink_addrs = {}
    request_meta = defaultdict(deque)   # source -> deque[metadata dict], oldest first
    last_completion = {}  # (source, opcode) -> last completion cycle
    last_source_d = {}    # (source, source_d_opcode) -> last Source D cycle seen
    results = []          # [(source, opcode, start, end, latency, metadata, stalls, source_d_cycle)]
    l1_start = {}         # (addr, core) -> cycle
    l1_data = {}          # (addr, core) -> cycle
    l1_results = []       # results list
    l1_release_start = {}   # (addr, core) -> New L1 Release cycle
    l1_release_results = [] # (addr, core, start, end, latency)
    l1_release_aru_results = [] # (addr, core, start, in_relu, RelUArbTime)

    # -----------------------------------------------------------------------
    # Probe tracking
    #
    # A single MSHR can launch multiple L1 probes. All of those probes share
    # the MSHR's start cycle, but each ProbeAck produces its own result.
    #
    # Probe batches are keyed by (set, tag):
    #
    #   probe_batches[(set, tag)] = [
    #       {
    #           "start":    MSHR start cycle,
    #           "source":   MSHR source,
    #           "expected": number of L1 Probe Req events,
    #           "received": number of ProbeAck events,
    #       },
    #       ...
    #   ]
    #
    # The list is ordered oldest -> newest.
    #
    # When a ProbeAck arrives, it is assigned to the MOST RECENT batch for
    # that (set, tag) which still has an outstanding probe.
    #
    # Each individual ProbeAck generates one probe_results entry, using the
    # MSHR batch's start cycle.
    # -----------------------------------------------------------------------
    probe_batches = defaultdict(list)
    probe_results = []                   # (set, start, end, latency, mshr_source)

    pending_source_d = {}  # src -> (opcode, start_cycle, end_cycle, latency, meta, stall_cycles)
    source_a_times = {}   # (src, opcode) -> start_cycle
    dram_results = []     # [(src, start, end, latency)]            (DRAM reads)
    l1_hits = defaultdict(int)  # core -> hit count

    # -----------------------------------------------------------------------
    # NEW: LLC MSHR occupancy
    #
    # Start: "Req in MSHR; ..."                              (keyed by source)
    # End:   "<AcquireBlock|Release|ReleaseData> Request completed; sent to
    #         directory! source: X"                          (same source)
    #
    # If a source shows up in "Req in MSHR" again before its completion was
    # seen, the newer start wins (so one missing completion line can't poison
    # every later request with that source) and the event is counted.
    # -----------------------------------------------------------------------
    llc_mshr_start = defaultdict(deque)        # src -> {"start": cycle, "need_dram": .., ...}
    llc_mshr_results = []      # [(src, opcode, start, end, occupancy, info)]
    llc_mshr_overwritten = 0

    # -----------------------------------------------------------------------
    # NEW: DRAM write time
    #
    # Start: first beat of "New Source C Request! opcode: ReleaseData"
    # End:   "New Sink D Request! opcode: ReleaseAck" with the same source
    #
    # Plain Release opcodes never open a write, so their ReleaseAcks find no
    # pending entry and are ignored.
    # -----------------------------------------------------------------------
    dram_write_start = {}      # src -> (start_cycle, addr or None)
    dram_write_results = []    # [(src, start, end, latency, addr or None)]
    source_c_data_no_source = 0   # ReleaseData Source C lines lacking a source field
    ignored_release_acks = 0      # Sink D ReleaseAcks with no pending ReleaseData write

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

    # -----------------------------------------------------------------------
    # In-flight overlap handling
    #
    # An L1 can free its MSHR (and reuse the source ID) before the LLC has logged
    # "Request completed" for the previous request with that source, so several
    # requests with the same (source, opcode) can be in flight at once. They are
    # kept in arrival order and each completion retires the oldest.
    #   sink_times[key]        = oldest in-flight Sink arrival; sink_addrs[key] its address
    #   sink_backlog[key]      = newer arrivals queued behind it
    #   request_meta[src]      = deque of "Req in MSHR" metadata, oldest first
    #   llc_mshr_start[src]    = deque of LLC MSHR starts, oldest first
    # Only single-beat opcodes are queued (every Sink A / Release is a new request).
    # ReleaseData is multi-beat, so it keeps first-beat-only + the cooldown above.
    # -----------------------------------------------------------------------
    SINGLE_BEAT_OPCODES = {"AcquireBlock", "AcquirePerm", "Release"}
    sink_backlog = defaultdict(deque)   # (src, opcode) -> deque[(cycle, addr)]
    sink_claims  = defaultdict(int)     # (src, opcode) -> in-flight sinks already paired with a meta line

    def claim_sink_for_meta(src, cycle):
        """Pair a 'Req in MSHR' line with the oldest in-flight Sink request of this
        source that has not been paired yet. Returns (start_cycle, addr) or None."""
        best = None
        for key in [k for k in sink_times if k[0] == src]:
            entries = [(sink_times[key], sink_addrs.get(key))] + list(sink_backlog.get(key, ()))
            c = sink_claims[key]
            if c < len(entries) and entries[c][0] <= cycle and (best is None or entries[c][0] < best[0][0]):
                best = (entries[c], key)
        if best is None:
            return None
        sink_claims[best[1]] += 1
        return best[0]

    def advance_sink(key):
        """Retire the oldest in-flight Sink request for key and promote the next."""
        del sink_times[key]
        sink_addrs.pop(key, None)
        sink_claims[key] = max(0, sink_claims[key] - 1)
        q = sink_backlog.get(key)
        if q:
            cyc, a = q.popleft()
            sink_times[key] = cyc
            if a is not None:
                sink_addrs[key] = a
            if not q:
                del sink_backlog[key]

    with open(filepath, "r") as f:
        for line_no, line in enumerate(f, 1):

            # ---------------------------------------------------------------
            # ProbeAck (Sink C)
            #
            # Match the ACK to the most recent MSHR probe batch for the same
            # (set, tag) which still has an outstanding probe.
            #
            # Each ACK produces its own result, but uses the MSHR batch's
            # start cycle.
            # ---------------------------------------------------------------
            if m := sink_c_probeack_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)

                s   = addr_set(addr)
                tag = addr_tag(addr)

                probe_key = (s, tag)
                batches = probe_batches.get(probe_key)

                if not batches:
                    if debug:
                        print(
                            f"[line {line_no}] UNMATCHED PROBE ACK "
                            f"addr=0x{addr:X}, "
                            f"set=0x{s:X}, tag=0x{tag:X}, "
                            f"cycle={cycle}"
                        )
                    continue

                # Search newest -> oldest.
                #
                # This implements the desired rule:
                # "attribute each ProbeAck to the most recent probe start
                # that matches the address."
                batch = None

                for candidate in reversed(batches):
                    if candidate["received"] < candidate["expected"]:
                        batch = candidate
                        break

                if batch is None:
                    if debug:
                        print(
                            f"[line {line_no}] PROBE ACK has no outstanding "
                            f"probe slot: addr=0x{addr:X}, "
                            f"set=0x{s:X}, tag=0x{tag:X}, "
                            f"cycle={cycle}"
                        )
                    continue

                # One ProbeAck completes one L1 probe.
                batch["received"] += 1

                start_cycle = batch["start"]
                mshr_source = batch["source"]
                latency = cycle - start_cycle

                probe_results.append(
                    (s, start_cycle, cycle, latency, mshr_source)
                )

                if debug:
                    print(
                        f"[line {line_no}] PROBE ACK "
                        f"addr=0x{addr:X}, "
                        f"set=0x{s:X}, tag=0x{tag:X}, "
                        f"-> MSHR source=0x{mshr_source:X}, "
                        f"start={start_cycle}, end={cycle}, "
                        f"lat={latency}, "
                        f"received={batch['received']}/"
                        f"{batch['expected']}"
                    )

                # Once all probes belonging to this MSHR have ACKed,
                # remove the batch.
                if batch["received"] == batch["expected"]:
                    batches.remove(batch)

                    if debug:
                        print(
                            f"[line {line_no}] PROBE BATCH COMPLETE "
                            f"set=0x{s:X}, tag=0x{tag:X}, "
                            f"source=0x{mshr_source:X}, "
                            f"start={start_cycle}, "
                            f"probes={batch['expected']}"
                        )

                    if not batches:
                        del probe_batches[probe_key]

                continue

            # --- Match Sink arrivals ---
            if m := sink_re.search(line):
                cycle = int(m[1])
                sink_type = m[2].upper()
                opcode = m[3]
                addr = int(m[4], 16) if m[4] else None
                src = int(m[5], 16)
                sink_key = (src, opcode)

                last = last_completion.get(sink_key)
                if (opcode == "ReleaseData" 
                        and last is not None 
                        and (cycle - last) <= COMPLETION_COOLDOWN):
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
                    if addr is not None:
                        sink_addrs[sink_key] = addr
                    if debug:
                        print(f"[line {line_no}] Sink {sink_type}: cycle={cycle}, opcode={opcode}, source=0x{src:X}")
                elif opcode in SINGLE_BEAT_OPCODES:
                    sink_backlog[sink_key].append((cycle, addr))
                    if debug:
                        print(f"[line {line_no}] Sink {sink_type}: cycle={cycle}, opcode={opcode}, "
                              f"source=0x{src:X} queued behind in-flight request "
                              f"(depth={len(sink_backlog[sink_key])})")
                else:
                    if debug:
                        print(f"[line {line_no}] Duplicate Sink {sink_type} for source=0x{src:X}, opcode={opcode}")
                continue

            # ---------------------------------------------------------------
            # MSHR metadata line — now also carries set and tag.
            # If need_probe is set, probe timing starts HERE (not at Source B).
            # This line is also the start of LLC MSHR occupancy.
            # ---------------------------------------------------------------
            if m := meta_re.search(line):
                cycle      = int(m[1])
                dram       = int(m[2])
                need_probe = int(m[3])
                evicting   = int(m[4])
                back_inv   = int(m[5])
                src        = int(m[6], 16)
                mshr_set   = int(m[7], 16)
                tag        = int(m[8], 16)
                param      = int(m[9], 16)
                node_type  = "Store" if param in (1, 2) else "Load"   # NEW

                # Look up the sink arrival time for this source so we can later
                # compute SetBlockTime = MSHR_entry_cycle - sink_arrival_cycle.
                # We search sink_times for any opcode matching this source because
                # the MSHR line doesn't carry the opcode directly.
                claim = claim_sink_for_meta(src, cycle)
                sink_start, address = claim if claim else (None, None)

                meta_entry = {
                    "need_dram":  dram,
                    "need_probe": need_probe,
                    "evicting":   evicting,
                    "back_inv":   back_inv,
                    "meta_cycle": cycle,
                    "set":        mshr_set,
                    "tag":        tag,
                    "sink_start": sink_start,
                    "address":    address,
                    "node_type":  node_type,     # NEW
                }
                request_meta[src].append(meta_entry)

                # NEW: LLC MSHR occupancy start (keyed by source)
                llc_mshr_start[src].append({
                    "start":      cycle,
                    "need_dram":  dram,
                    "need_probe": need_probe,
                    "evicting":   evicting,
                    "back_inv":   back_inv,
                    "set":        mshr_set,
                    "tag":        tag,
                    "param":      param,
                })

                if need_probe:
                    # Create a new probe batch. The MSHR cycle is the start
                    # time for EVERY L1 probe launched by this batch.
                    probe_key = (mshr_set, tag)

                    probe_batches[probe_key].append({
                        "start": cycle,
                        "source": src,
                        "expected": 0,
                        "received": 0,
                    })

                    if debug:
                        print(
                            f"[line {line_no}] PROBE BATCH START "
                            f"set=0x{mshr_set:X}, tag=0x{tag:X}, "
                            f"source=0x{src:X}, cycle={cycle}"
                        )

                if debug:
                    print(f"[line {line_no}] Metadata captured for source=0x{src:X}: {meta_entry}")
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

                # NEW: LLC MSHR occupancy end. Done first and independently of the
                # sink/Source D matching below, so it is never affected by the
                # duplicate/cooldown/rejection logic. Popping makes a repeated
                # completion line for the same source a no-op.
                # LLC MSHR occupancy end (FIFO)
                # LLC MSHR occupancy end (FIFO), independent of sink/Source D matching.
                # Consume the oldest outstanding start for this source.
                mshr_queue = llc_mshr_start.get(src)

                if mshr_queue:
                    mshr_info = mshr_queue.popleft()

                    occupancy = cycle - mshr_info["start"]
                    llc_mshr_results.append(
                        (src, opcode, mshr_info["start"], cycle, occupancy, mshr_info)
                    )

                    if debug:
                        print(
                            f"[line {line_no}] LLC MSHR occupancy: "
                            f"source=0x{src:X}, opcode={opcode}, "
                            f"start={mshr_info['start']}, end={cycle}, "
                            f"occupancy={occupancy}"
                        )

                    # Remove the source key if no starts remain.
                    if not mshr_queue:
                        del llc_mshr_start[src]

                elif debug:
                    print(
                        f"[line {line_no}] LLC MSHR completion with no 'Req in MSHR': "
                        f"source=0x{src:X}, opcode={opcode}, cycle={cycle}"
                    )

                # Retire the oldest MSHR metadata for this source (FIFO, kept aligned with
                # the LLC MSHR queue above regardless of sink/Source D matching below).
                meta_q = request_meta.get(src)
                meta_info = meta_q.popleft() if meta_q else {}
                if meta_q is not None and len(meta_q) == 0:
                    del request_meta[src]

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
                meta = meta_info

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

                    advance_sink(sink_key)
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

                    advance_sink(sink_key)
                    last_completion[sink_key] = cycle

            # ---------------------------------------------------------------
            # L1 Probe Request
            #
            # Each of these represents one actual probe launched by an MSHR.
            # Increment the expected count of the most recent matching MSHR
            # probe batch.
            # ---------------------------------------------------------------
            if m := l1_probe_new_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)

                s   = addr_set(addr)
                tag = addr_tag(addr)

                probe_key = (s, tag)
                batches = probe_batches.get(probe_key)

                if not batches:
                    if debug:
                        print(
                            f"[line {line_no}] UNMATCHED L1 PROBE REQ "
                            f"addr=0x{addr:X}, "
                            f"set=0x{s:X}, tag=0x{tag:X}, "
                            f"core=0x{core:X}, cycle={cycle}"
                        )
                else:
                    # The newest MSHR batch owns newly-launched probes.
                    batch = batches[-1]
                    batch["expected"] += 1

                    if debug:
                        print(
                            f"[line {line_no}] PROBE REQ "
                            f"addr=0x{addr:X}, core=0x{core:X}, "
                            f"set=0x{s:X}, tag=0x{tag:X}, "
                            f"-> MSHR source=0x{batch['source']:X}, "
                            f"start={batch['start']}, "
                            f"expected={batch['expected']}"
                        )

                continue

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

            # --- L1 MSHR free: end of L1 MSHR occupancy (New L1 Request -> here) ---
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

            # --- L1 release enters the release unit: arbitration delay ---
            if m := l1_release_in_re.search(line):
                cycle = int(m[1])
                addr  = int(m[2], 16)
                core  = int(m[3], 16)
                key   = (addr, core)

                if key in l1_release_start:
                    start = l1_release_start[key]
                    arb_time = cycle - start
                    l1_release_aru_results.append(
                        (addr, core, start, cycle, arb_time)
                    )
                    if debug:
                        print(
                            f"[line {line_no}] RelUArbTime addr=0x{addr:X}, "
                            f"core=0x{core:X}, start={start}, end={cycle}, "
                            f"RelUArbTime={arb_time}"
                        )
                elif debug:
                    print(
                        f"[line {line_no}] L1 Release In RelU unmatched "
                        f"addr=0x{addr:X}, core=0x{core:X}, cycle={cycle}"
                    )
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

            # Memory timing (reads): Source A AcquireBlock -> Sink D GrantData
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

            # NEW: Memory timing (writes): Source C ReleaseData -> Sink D ReleaseAck
            if m := source_c_re.search(line):
                cycle  = int(m[1])
                opcode = m[2]
                rest   = m[3]
                if opcode == "ReleaseData":
                    sm = source_field_re.search(rest)
                    if sm is None:
                        # Log line has no source field yet; can't match this write.
                        source_c_data_no_source += 1
                        if debug:
                            print(f"[line {line_no}] Source C ReleaseData has no source field, "
                                  f"cycle={cycle}")
                    else:
                        src = int(sm[1], 16)
                        # ReleaseData is multi-beat: only the first beat starts the timer.
                        if src not in dram_write_start:
                            am = addr_field_re.search(rest)
                            dram_write_start[src] = (cycle, int(am[1], 16) if am else None)
                            if debug:
                                print(f"[line {line_no}] DRAM write start: source=0x{src:X}, cycle={cycle}")
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
                elif opcode == "ReleaseAck":
                    # ReleaseAcks are also issued for plain Releases (no DRAM write);
                    # those have no pending ReleaseData entry and are ignored.
                    w = dram_write_start.pop(src, None)
                    if w is not None:
                        start, waddr = w
                        latency = cycle - start
                        dram_write_results.append((src, start, cycle, latency, waddr))
                        if debug:
                            print(f"[line {line_no}] DRAM write complete: source=0x{src:X}, "
                                  f"start={start}, end={cycle}, latency={latency}")
                    else:
                        ignored_release_acks += 1
                        if debug:
                            print(f"[line {line_no}] Sink D ReleaseAck source=0x{src:X} has no "
                                  f"pending ReleaseData write (plain Release?) — ignored")
                continue

    # --- CSV output ---
    if csv_out:
        with open(csv_out, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "Opcode", "StartCycle", "EndCycle", "Latency",
                 "SetBlockTime", "ReleaseStallCycles", "SourceDCycle", "SourceDToComplete",
                 "NeedDRAM", "NeedProbe", "Evicting", "BackInv", "Address", "NodeType"])  # NEW
            for src, opcode, start, end, lat, meta, stalls, source_d_cycle in results:
                d_to_complete = (end - source_d_cycle) if source_d_cycle is not None else ""
                meta_cycle  = meta.get('meta_cycle')
                sink_start  = meta.get('sink_start')
                address     = meta.get('address')
                set_block_time = (meta_cycle - sink_start
                                  if meta_cycle is not None and sink_start is not None
                                  else "")
                writer.writerow([
                    f"0x{src:X}", opcode, start, end, lat,
                    set_block_time, stalls,
                    source_d_cycle if source_d_cycle is not None else "",
                    d_to_complete,
                    meta.get('need_dram',''), meta.get('need_probe',''),
                    meta.get('evicting',''), meta.get('back_inv',''),
                    f"0x{address:X}" if address is not None else "",
                    meta.get('node_type', ''),                                          # NEW
                ])
        print(f"\n Results with metadata written to {csv_out}")

        # DRAM reads (unchanged file name / schema)
        dram_csv = csv_out.replace(".csv", "-dram.csv")
        with open(dram_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "StartCycle", "EndCycle", "Latency"])
            for src, start, end, lat in dram_results:
                writer.writerow([f"0x{src:X}", start, end, lat])
        print(f"\n DRAM read results written to {dram_csv}")

        # NEW: DRAM writes
        dram_write_csv = csv_out.replace(".csv", "-dram-write.csv")
        with open(dram_write_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "StartCycle", "EndCycle", "Latency", "Address"])
            for src, start, end, lat, waddr in dram_write_results:
                writer.writerow([f"0x{src:X}", start, end, lat,
                                 f"0x{waddr:X}" if waddr is not None else ""])
        print(f"\n DRAM write results written to {dram_write_csv}")

        # NEW: LLC MSHR occupancy
        llc_mshr_csv = csv_out.replace(".csv", "-llc-mshr.csv")
        with open(llc_mshr_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "Opcode", "StartCycle", "EndCycle", "Occupancy",
                             "NeedDRAM", "NeedProbe", "Evicting", "BackInv",
                             "Set", "Tag", "Param"])
            for src, opcode, start, end, occ, info in llc_mshr_results:
                writer.writerow([
                    f"0x{src:X}", opcode, start, end, occ,
                    info["need_dram"], info["need_probe"], info["evicting"], info["back_inv"],
                    f"0x{info['set']:X}", f"0x{info['tag']:X}", f"0x{info['param']:X}",
                ])
        print(f"\n LLC MSHR occupancy written to {llc_mshr_csv}")

    # --- Summary stats ---
    print(f"\nSummary:")
    print(f"  {len(results)} completions matched")
    print(f"  {len(sink_times)} sinks still pending "
          f"(+ {sum(len(q) for q in sink_backlog.values())} queued behind them)")
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

        release_aru_csv = l1_out.replace(".csv", "_release_aru.csv")
        with open(release_aru_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Address", "Core", "StartCycle", "InRelUCycle", "RelUArbTime"])
            for addr, core, start, in_relu, arb_time in l1_release_aru_results:
                writer.writerow([
                    f"{addr:#X}", f"0x{core:X}", start, in_relu, arb_time
                ])
        print(f"\nRelU arbitration results written to {release_aru_csv}")

        print(f"\nL1 Release Summary:")
        print(f"  {len(l1_release_results)} L1 releases completed")
        print(f"  {len(l1_release_start)} L1 releases still pending")
        print(f"  {len(l1_release_aru_results)} RelU arbitration times captured")

        # Probe CSV now uses Set as the key instead of Address
        probe_csv = csv_out.replace(".csv", "-probes.csv")
        with open(probe_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Set", "MSHRSource", "StartCycle", "EndCycle", "Latency"])
            for s, start, end, lat, mshr_source in probe_results:
                writer.writerow([f"0x{s:X}", f"0x{mshr_source:X}", start, end, lat])
        print(f"\nProbe results written to {probe_csv}")

        pending_probe_count = sum(
            1
            for batches in probe_batches.values()
            for batch in batches
            if batch["received"] < batch["expected"]
        )
        print(f"\nL1 Probe Summary:")
        print(f"  {len(probe_results)} Probes completed")
        print(f"  {pending_probe_count} Probes still pending")

    # NEW: LLC MSHR + DRAM summaries
    print(f"\nLLC MSHR Occupancy Summary:")
    print(f"  {len(llc_mshr_results)} requests completed")
    print(f"  {sum(len(q) for q in llc_mshr_start.values())} still pending (no completion seen)")
    print(f"  {sum(len(q) for q in request_meta.values())} MSHR metadata entries left queued "
          f"(large numbers mean completions are missing and the queues have drifted)")
    if llc_mshr_overwritten:
        print(f"  [WARN] {llc_mshr_overwritten} 'Req in MSHR' starts were overwritten before a "
              f"completion was seen (duplicate lines or missing completions)")

    print(f"\nDRAM Summary:")
    print(f"  {len(dram_results)} reads completed | {len(source_a_times)} reads pending")
    print(f"  {len(dram_write_results)} writes completed | {len(dram_write_start)} writes pending")
    print(f"  {ignored_release_acks} Sink D ReleaseAcks ignored (no pending ReleaseData write)")
    if source_c_data_no_source:
        print(f"  [WARN] {source_c_data_no_source} 'Source C ReleaseData' lines had no 'source:' field, "
              f"so they could not be timed. Add source to that log line.")

    # Optional: pending warnings in debug mode
    if debug and sink_times:
        print("\n [WARN] Pending sinks (unmatched requests):")
        for (src, opcode), start_cycle in sorted(sink_times.items(), key=lambda kv: kv[1]):
            print(f"    - Source 0x{src:X}, opcode={opcode}, issued @ {start_cycle}")

    if debug:
        pending_probe_count = sum(
            1
            for batches in probe_batches.values()
            for batch in batches
            if batch["received"] < batch["expected"]
        )
        if pending_probe_count:
            print("\n [WARN] Pending probes (unmatched):")

            for (s, tag), batches in sorted(probe_batches.items()):
                for batch in batches:
                    outstanding = (
                        batch["expected"] - batch["received"]
                    )

                    if outstanding > 0:
                        print(
                            f"    - Set=0x{s:X}, "
                            f"Tag=0x{tag:X}, "
                            f"mshr_source=0x{batch['source']:X}, "
                            f"started @ {batch['start']}, "
                            f"probes={batch['received']}/"
                            f"{batch['expected']} "
                            f"({outstanding} outstanding)"
                        )

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

    # RelU arbitration times
    if l1_release_aru_results:
        by_arb = sorted(l1_release_aru_results, key=lambda r: r[4])
        for label, r in [("RelUArbTime min", by_arb[0]), ("RelUArbTime max", by_arb[-1])]:
            addr, core, start, in_relu, arb_time = r
            print(f"  {label}: addr=0x{addr:X}, core=0x{core:X}, "
                  f"start={start}, end={in_relu}, RelUArbTime={arb_time}")

    # Probes
    if probe_results:
        by_lat = sorted(probe_results, key=lambda r: r[3])
        for label, r in [("Probe min", by_lat[0]), ("Probe max", by_lat[-1])]:
            s, start, end, lat, mshr_source = r
            print(f"  {label}: set=0x{s:X}, mshr_source=0x{mshr_source:X}, "
                  f"start={start}, end={end}, latency={lat}")

    # NEW: LLC MSHR occupancy
    if llc_mshr_results:
        by_occ = sorted(llc_mshr_results, key=lambda r: r[4])
        for label, r in [("LLC MSHR min", by_occ[0]), ("LLC MSHR max", by_occ[-1])]:
            src, opcode, start, end, occ, info = r
            print(f"  {label}: opcode={opcode}, source=0x{src:X}, start={start}, end={end}, occupancy={occ}")

    # NEW: DRAM reads / writes
    if dram_results:
        by_lat = sorted(dram_results, key=lambda r: r[3])
        for label, r in [("DRAM read min", by_lat[0]), ("DRAM read max", by_lat[-1])]:
            src, start, end, lat = r
            print(f"  {label}: source=0x{src:X}, start={start}, end={end}, latency={lat}")
    if dram_write_results:
        by_lat = sorted(dram_write_results, key=lambda r: r[3])
        for label, r in [("DRAM write min", by_lat[0]), ("DRAM write max", by_lat[-1])]:
            src, start, end, lat, waddr = r
            print(f"  {label}: source=0x{src:X}, start={start}, end={end}, latency={lat}")

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
                    "including ReleaseData stall tracking and metadata. Also extracts "
                    "L1 miss penalty / MSHR occupancy, LLC MSHR occupancy, probe time, "
                    "and DRAM read/write time."
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