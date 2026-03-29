#!/usr/bin/env python3
import re
import argparse
import csv
import os
from collections import defaultdict

def parse_log(filepath, csv_out=None, l1_out=None, debug=False):
    # Regex patterns
    sink_re = re.compile(
        r"@ clk_cycle\s+(\d+): New Sink ([ACX]) Request! opcode:\s*(\w+).*source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)
    meta_re = re.compile(
        r"@ clk_cycle\s+(\d+): Req in MSHR; need dram\?: (\d+), need probe\? (\d+), evicting\? (\d+), back-inv\? (\d+), source: (0x[0-9a-fA-F]+)",
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


    # --- State maps ---
    sink_times = {}       # (source, opcode) -> start_cycle
    request_meta = {}     # source -> metadata dict
    last_completion = {}  # (source, opcode) -> last completion cycle
    last_source_d = {}    # source -> last Source D cycle seen
    results = []          # [(source, opcode, start, end, latency, metadata, stalls, source_d_cycle)]
    l1_start = {}         # (addr, core) -> cycle
    l1_data = {}          # (addr, core) -> cycle
    l1_results = []       # results list
    l1_release_start = {}   # (addr, core) -> cycle
    l1_release_results = [] # (addr, core, start, end, latency)


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

            # --- Match metadata lines ---
            if m := meta_re.search(line):
                cycle = int(m[1])
                dram = int(m[2])
                probe = int(m[3])
                evicting = int(m[4])
                back_inv = int(m[5])
                src = int(m[6], 16)

                # Metadata can't infer opcode directly; attach later by matching nearby Sink
                # So store by source temporarily
                request_meta[src] = {
                    "need_dram": dram,
                    "need_probe": probe,
                    "evicting": evicting,
                    "back_inv": back_inv,
                    "meta_cycle": cycle
                }
                if debug:
                    print(f"[line {line_no}] Metadata captured for source=0x{src:X}: {request_meta[src]}")
                continue

            # --- Match ReleaseData stalls ---
            if m := stall_re.search(line):
                cycle = int(m[1])
                release_stalls["pending"]["last_cycle"] = cycle
                release_stalls["pending"]["count"] += 1
                if debug:
                    print(f"[line {line_no}] ReleaseData stall at cycle={cycle} (count={release_stalls['pending']['count']})")
                continue

            # --- Match Source D (track last cycle per source) ---
            if m := source_d_re.search(line):
                cycle = int(m[1])
                src = int(m[3], 16)
                last_source_d[src] = cycle
                if debug:
                    print(f"[line {line_no}] Source D: cycle={cycle}, source=0x{src:X} (last updated)")
                continue

            # --- Match completions ---
            if m := complete_re.search(line):
                cycle = int(m[1])
                opcode = m[2]
                src = int(m[3], 16)
                sink_key = (src, opcode)

                # Skip duplicate completions within short window
                last = last_completion.get(sink_key)
                if last is not None and (cycle - last) < DUPLICATE_COMPLETION_WINDOW:
                    if debug:
                        print(f"[line {line_no}] Duplicate completion suppressed for {sink_key}, cycle={cycle}, last={last}")
                    last_completion[sink_key] = cycle
                    continue

                # Attach pending stall info for ReleaseData
                if opcode == "ReleaseData":
                    stall_info = release_stalls.pop("pending", None)
                    stall_cycles = stall_info["count"] if stall_info else 0
                else:
                    stall_cycles = 0

                # Grab last Source D cycle for this source (then clear it)
                source_d_cycle = last_source_d.pop(src, None)

                # Compute latency
                if sink_key in sink_times:
                    start_cycle = sink_times[sink_key]

                    # Discard stale Source D from a prior request on the same source
                    if source_d_cycle is not None and source_d_cycle < start_cycle:
                        if debug:
                            print(f"[line {line_no}] Discarding stale Source D cycle {source_d_cycle} "
                                  f"for source=0x{src:X} (before start={start_cycle})")
                        source_d_cycle = None
                    latency = cycle - start_cycle

                    # Match metadata (by source only)
                    meta = request_meta.get(src, {})

                    results.append((src, opcode, start_cycle, cycle, latency, meta, stall_cycles, source_d_cycle))

                    if debug:
                        source_d_to_complete = (cycle - source_d_cycle) if source_d_cycle is not None else "-"
                        print(f"[line {line_no}] Completed: source=0x{src:X}, opcode={opcode}, start={start_cycle}, end={cycle}, "
                              f"latency={latency}, stalls={stall_cycles}, source_d={source_d_cycle}, "
                              f"source_d_to_complete={source_d_to_complete}, metadata={meta}")

                    del sink_times[sink_key]
                else:
                    if debug:
                        print(f"[line {line_no}] Completion unmatched: source=0x{src:X}, opcode={opcode}, cycle={cycle}")

                last_completion[sink_key] = cycle
                continue

            # --- L1 new requests ---
            if m := l1_new_re.search(line):
                cycle = int(m[1])
                addr = int(m[2], 16)
                core = int(m[3], 16)

                key = (addr, core)

                if key not in l1_start:
                    l1_start[key] = cycle
                    if debug:
                        print(f"[line {line_no}] L1 NEW addr=0x{addr:X}, core=0x{core:X}, cycle={cycle}")
                continue

            # --- L1 data to core ---
            if m := l1_data_re.search(line):
                cycle = int(m[1])
                addr = int(m[2], 16)
                core = int(m[3], 16)

                key = (addr, core)
                l1_data[key] = cycle

                if debug:
                    print(f"[line {line_no}] L1 DATA addr=0x{addr:X}, cycle={cycle}")
                continue

            if m := l1_free_re.search(line):
                cycle = int(m[1])
                addr = int(m[2], 16)
                core = int(m[3], 16)

                key = (addr, core)

                if key in l1_start:
                    start = l1_start[key]
                    latency = cycle - start
                    data_cycle = l1_data.get(key)

                    l1_results.append(
                        (addr, core, start, data_cycle, cycle, latency)
                    )

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
                addr = int(m[2], 16)
                core = int(m[3], 16)
                key = (addr, core)
                if key not in l1_release_start:
                    l1_release_start[key] = cycle
                    if debug:
                        print(f"[line {line_no}] L1 RELEASE NEW addr=0x{addr:X}, core=0x{core:X}, cycle={cycle}")
                continue

            if m := l1_release_complete_re.search(line):
                cycle = int(m[1])
                addr = int(m[2], 16)
                core = int(m[3], 16)
                key = (addr, core)
                if key in l1_release_start:
                    start = l1_release_start[key]
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


    # --- Output summary ---
    header = f"\n{'Source':>8}  {'Opcode':>12}  {'Start':>8}  {'End':>8}  {'Latency':>8}  {'Stalls':>6}  {'SourceD':>8}  {'D→Done':>6}  {'Metadata'}"
    print(header)
    print("-" * len(header))
    for src, opcode, start, end, lat, meta, stalls, source_d_cycle in sorted(results, key=lambda r: r[2]):
        src_str = f"0x{src:X}"
        meta_str = f"DRAM={meta.get('need_dram','-')}, Probe={meta.get('need_probe','-')}, Evict={meta.get('evicting','-')}, BackInv={meta.get('back_inv','-')}"
        source_d_str = str(source_d_cycle) if source_d_cycle is not None else "-"
        d_to_done_str = str(end - source_d_cycle) if source_d_cycle is not None else "-"
        print(f"{src_str:<8}  {opcode:<12}  {start:8d}  {end:8d}  {lat:8d}  {stalls:6d}  {source_d_str:>8}  {d_to_done_str:>6}  {meta_str}")

    # --- CSV output ---
    if csv_out:
        with open(csv_out, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "Opcode", "StartCycle", "EndCycle", "Latency", "ReleaseStallCycles",
                             "SourceDCycle", "SourceDToComplete",
                             "NeedDRAM", "NeedProbe", "Evicting", "BackInv"])
            for src, opcode, start, end, lat, meta, stalls, source_d_cycle in results:
                d_to_complete = (end - source_d_cycle) if source_d_cycle is not None else ""
                writer.writerow([
                    f"0x{src:X}", opcode, start, end, lat, stalls,
                    source_d_cycle if source_d_cycle is not None else "",
                    d_to_complete,
                    meta.get('need_dram',''), meta.get('need_probe',''),
                    meta.get('evicting',''), meta.get('back_inv','')
                ])
        print(f"\n Results with metadata written to {csv_out}")

    # --- Summary stats ---
    print(f"\nSummary:")
    print(f"  {len(results)} completions matched")
    print(f"  {len(sink_times)} sinks still pending")
    print(f"  Sink C total: {total_sink_c} | accepted: {accepted_sink_c} | ignored: {ignored_sink_c}")

    if l1_out:
        print("\nL1 Latencies:")
        print(f"{'Address':>12} {'Core':>6} {'Start':>8} {'Data':>8} {'End':>8} {'Latency':>8} {'MissPenalty':>8}")
        print("-" * 60)

        for addr, core, start, data, end, lat in sorted(l1_results, key=lambda r: r[2]):
            data_str = data if data is not None else "-"
            miss_penalty = data - start
            print(f"0x{addr:0>8} 0x{core:0>1} {start:8d} {data_str:8d} {end:8d} {lat:8d} {miss_penalty:8d}")
    
    # --- L1 CSV output ---
    if l1_out:
        os.makedirs(os.path.dirname(l1_out), exist_ok=True)

        with open(l1_out, "w", newline="") as fout:
            writer = csv.writer(fout)

            writer.writerow(["Address","Core","StartCycle","DataCycle","EndCycle","Latency","MissPenalty"])

            for addr, core, start, data, end, lat in l1_results:

                miss_penalty = (data - start if data is not None else "")

                writer.writerow([
                    f"{addr:#X}",f"0x{core:X}",start,
                    data if data is not None else "",
                    end,lat,miss_penalty
                ])

        print(f"\nL1 results written to {l1_out}")


    print(f"\nL1 Summary:")
    print(f"  {len(l1_results)} L1 requests completed")
    print(f"  {len(l1_start)} L1 requests still pending")

    if l1_out:  # or a new dedicated arg
        print("\nL1 Release Latencies:")
        print(f"{'Address':>12} {'Core':>6} {'Start':>8} {'End':>8} {'Latency':>8}")
        print("-" * 50)
        for addr, core, start, end, lat in sorted(l1_release_results, key=lambda r: r[2]):
            print(f"0x{addr:0>8} 0x{core:0>1} {start:8d} {end:8d} {lat:8d}")

        release_csv = l1_out.replace(".csv", "_releases.csv")  # or a new arg
        with open(release_csv, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["Address", "Core", "StartCycle", "EndCycle", "Latency"])
            for addr, core, start, end, lat in l1_release_results:
                writer.writerow([f"{addr:#X}", f"0x{core:X}", start, end, lat])
        print(f"\nL1 Release results written to {release_csv}")

    print(f"\nL1 Release Summary:")
    print(f"  {len(l1_release_results)} L1 releases completed")
    print(f"  {len(l1_release_start)} L1 releases still pending")


    # Optional: print pending list if debug enabled
    if debug and sink_times:
        print("\n [WARN] Pending sinks (unmatched requests):")
        for (src, opcode), start_cycle in sorted(sink_times.items(), key=lambda kv: kv[1]):
            print(f"    - Source 0x{src:X}, opcode={opcode}, issued @ {start_cycle}")


    return results


def main():
    parser = argparse.ArgumentParser(
        description="Compute request processing latency using (source, opcode) keys, including ReleaseData stall tracking and metadata."
    )
    parser.add_argument("logfile", help="Path to the log file (with Sink + completion lines)")
    parser.add_argument("--csv", help="Optional output CSV file path")
    parser.add_argument("--l1csv", help="Optional output L1 CSV file path")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug printing")
    args = parser.parse_args()

    if not os.path.isfile(args.logfile):
        print(f"Error: file '{args.logfile}' not found.")
        return

    parse_log(args.logfile, csv_out=args.csv, l1_out=args.l1csv, debug=args.debug)


if __name__ == "__main__":
    main()