import glob
import os
import sys
import subprocess

def process_files(base_dir):
    pattern = os.path.join(base_dir, "VTestDriver__Trace__*__Slow.cpp")
    files = glob.glob(pattern)

    if not files:
        print("No matching files found.")
        return

    for file in files:
        print(f"Processing: {file}")
        subprocess.run(["sed", "-i", "s/:,/:LONGINT,/g", file], check=True)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python fix_files.py <base_directory>")
        sys.exit(1)

    base_directory = sys.argv[1]
    process_files(base_directory)
