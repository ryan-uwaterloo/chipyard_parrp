#!/bin/bash
set -e  # exit on error

SRC="$1"
PREFIX="$2"
MODIFIER="$3"

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 <src_dir> <prefix> <modifier>"
    exit 1
fi

for f in ${SRC}/${PREFIX}.*.csv; do
    [[ -e "$f" ]] || { echo "No files matched"; exit 1; }
    mv "$f" "./${PREFIX}${MODIFIER}.$(basename "$f" | sed "s/${PREFIX}\.//")"
done