#!/usr/bin/env python3
import csv
import argparse
from collections import defaultdict

def load_csv(filepath):
    entries = defaultdict(list)
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                sid = int(row["SourceID"], 16)
                opc = row["Opcode"].strip()
                start = int(row["StartCycle"])
                end = int(row["EndCycle"])
                lat = int(row["Latency"])
                entries[(sid, opc)].append((start, end, lat))
            except Exception:
                # skip header or malformed lines
                continue
    return entries

def compare(base_file, new_file, csv_out=None):
    base = load_csv(base_file)
    new = load_csv(new_file)

    all_keys = sorted(set(base.keys()) | set(new.keys()), key=lambda x: (x[0], x[1]))
    results = []

    # Preserve chronological order based on base file
    base_order = []
    seen_keys = set()
    with open(base_file) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                sid = int(row["SourceID"], 16)
                opc = row["Opcode"].strip()
                key = (sid, opc)
                if key not in seen_keys:
                    base_order.append(key)
                    seen_keys.add(key)
            except Exception:
                continue

    for key in base_order:

        sid, opc = key
        b_list = base.get(key, [])
        n_list = new.get(key, [])
        for i, (b_start, b_end, b_lat) in enumerate(b_list):
            if i < len(n_list):
                n_start, n_end, n_lat = n_list[i]
                diff = n_lat - b_lat
                results.append((sid, opc, b_lat, n_lat, diff))
            else:
                results.append((sid, opc, b_lat, None, None))

    # Output
    print(f"{'Source':>8}  {'Opcode':>12}  {'BaseLat':>8}  {'NewLat':>8}  {'Δ':>6}")
    print("-" * 50)
    for sid, opc, b_lat, n_lat, diff in results:
        diff_str = f"{diff:+}" if diff is not None else ""
        new_lat_str = f"{n_lat}" if n_lat is not None else "-"
        src_str = f"0x{sid:X}"
        print(f"{src_str:<8}  {opc:<12}  {b_lat:8d}  {new_lat_str:>8}  {diff_str:>6}")


    # Write CSV if requested
    if csv_out:
        with open(csv_out, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["SourceID", "Opcode", "BaseLatency", "NewLatency", "Delta"])
            for sid, opc, b_lat, n_lat, diff in results:
                writer.writerow([f"0x{sid:X}", opc, b_lat, n_lat if n_lat is not None else "", diff if diff is not None else ""])

def main():
    parser = argparse.ArgumentParser(description="Compare MSHR timing results between two runs.")
    parser.add_argument("base", help="Base CSV file")
    parser.add_argument("new", help="New CSV file to compare against base")
    parser.add_argument("--csv", help="Output CSV for differences")
    args = parser.parse_args()

    compare(args.base, args.new, csv_out=args.csv)

if __name__ == "__main__":
    main()
