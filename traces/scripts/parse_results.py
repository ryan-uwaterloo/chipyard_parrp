#!/usr/bin/env python3
import re
import argparse
import csv
import os
from collections import defaultdict

def parse_log(filepath, csv_out=None, debug=False):
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
    # NEW: completion line now includes opcode before "Request completed"
    complete_re = re.compile(
        r"@ clk_cycle\s+(\d+):\s*(\w+)\s+Request completed; sent to directory!\s*source:\s*(0x[0-9a-fA-F]+)",
        re.IGNORECASE)

    # --- State maps ---
    sink_times = {}       # (source, opcode) -> start_cycle
    request_meta = {}     # (source, opcode) -> metadata dict
    last_completion = {}  # (source, opcode) -> last completion cycle
    results = []          # [(source, opcode, start, end, latency, metadata, stalls)]

    # --- Stall tracking ---
    release_stalls = defaultdict(lambda: {"last_cycle": None, "count": 0})

    # --- Stats ---
    total_sink_c = 0
    ignored_sink_c = 0
    accepted_sink_c = 0

    DUPLICATE_COMPLETION_WINDOW = 2

    with open(filepath, "r") as f:
        for line_no, line in enumerate(f, 1):

            # --- Match Sink arrivals ---
            if m := sink_re.search(line):
                cycle = int(m[1])
                sink_type = m[2].upper()
                opcode = m[3]
                src = int(m[4], 16)
                sink_key = (src, opcode)

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

                # Compute latency
                if sink_key in sink_times:
                    start_cycle = sink_times[sink_key]
                    latency = cycle - start_cycle

                    # Match metadata (by source only)
                    meta = request_meta.get(src, {})

                    results.append((src, opcode, start_cycle, cycle, latency, meta, stall_cycles))

                    if debug:
                        print(f"[line {line_no}] Completed: source=0x{src:X}, opcode={opcode}, start={start_cycle}, end={cycle}, "
                              f"latency={latency}, stalls={stall_cycles}, metadata={meta}")

                    del sink_times[sink_key]
                else:
                    if debug:
                        print(f"[line {line_no}] Completion unmatched: source=0x{src:X}, opcode={opcode}, cycle={cycle}")

                last_completion[sink_key] = cycle
                continue

    # --- Output summary ---
    header = f"\n{'Source':>8}  {'Opcode':>12}  {'Start':>8}  {'End':>8}  {'Latency':>8}  {'Stalls':>6}  {'Metadata'}"
    print(header)
    print("-" * len(header))
    for src, opcode, start, end, lat, meta, stalls in sorted(results, key=lambda r: r[2]):
        src_str = f"0x{src:X}"
        meta_str = f"DRAM={meta.get('need_dram','-')}, Probe={meta.get('need_probe','-')}, Evict={meta.get('evicting','-')}, BackInv={meta.get('back_inv','-')}"
        print(f"{src_str:<8}  {opcode:<12}  {start:8d}  {end:8d}  {lat:8d}  {stalls:6d}  {meta_str}")

    # --- CSV output ---
    if csv_out:
        with open(csv_out, "w", newline="") as fout:
            writer = csv.writer(fout)
            writer.writerow(["SourceID", "Opcode", "StartCycle", "EndCycle", "Latency", "ReleaseStallCycles",
                             "NeedDRAM", "NeedProbe", "Evicting", "BackInv"])
            for src, opcode, start, end, lat, meta, stalls in results:
                writer.writerow([
                    f"0x{src:X}", opcode, start, end, lat, stalls,
                    meta.get('need_dram',''), meta.get('need_probe',''),
                    meta.get('evicting',''), meta.get('back_inv','')
                ])
        print(f"\n✅ Results with metadata written to {csv_out}")

    # --- Summary stats ---
    print(f"\nSummary:")
    print(f"  {len(results)} completions matched")
    print(f"  {len(sink_times)} sinks still pending")
    print(f"  Sink C total: {total_sink_c} | accepted: {accepted_sink_c} | ignored: {ignored_sink_c}")

    # Optional: print pending list if debug enabled
    if debug and sink_times:
        print("\n⚠️  Pending sinks (unmatched requests):")
        for (src, opcode), start_cycle in sorted(sink_times.items(), key=lambda kv: kv[1]):
            print(f"    - Source 0x{src:X}, opcode={opcode}, issued @ {start_cycle}")


    return results


def main():
    parser = argparse.ArgumentParser(
        description="Compute request processing latency using (source, opcode) keys, including ReleaseData stall tracking and metadata."
    )
    parser.add_argument("logfile", help="Path to the log file (with Sink + completion lines)")
    parser.add_argument("--csv", help="Optional output CSV file path")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug printing")
    args = parser.parse_args()

    if not os.path.isfile(args.logfile):
        print(f"Error: file '{args.logfile}' not found.")
        return

    parse_log(args.logfile, csv_out=args.csv, debug=args.debug)


if __name__ == "__main__":
    main()
