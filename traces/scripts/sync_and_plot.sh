#!/bin/bash

for t in inter-iso inter-int hol probe nmshrs relbuf; do
    sftp rpsrvr1 <<EOF
lcd ../parsed/synthetics
cd work/chipyard/traces/parsed/synthetics
mget ${t}*.csv

lcd ../../mrt_data
cd ../../mrt_data
mget ${t}*.csv

bye
EOF
done

python3 plot_per_test.py
python3 plot_iso_int.py