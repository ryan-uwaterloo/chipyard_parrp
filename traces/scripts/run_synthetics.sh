mode=parrp

cd ../..
for t in probe nmshrs hol relbuf; do
  TRACE_DIR=./traces sbt -mem 6000 "project chipyard" \
      "testOnly chipyard.ProtoTest -- -z Synthetic-$t-4 -oD" \
      2> traces/logs/synthetics/$t-4-$mode.log

  python3 traces/scripts/parse_results.py traces/logs/synthetics/$t-4-$mode.log \
    --csv traces/parsed/synthetics/$t-4-$mode.csv \
    --l1csv traces/parsed/synthetics/$t-4-l1-$mode.csv

  cd traces/mrt_data
  ./get_mrt.sh ../../test_run_dir/mem_req_times/ $t-4 -$mode
  cd ../..
done