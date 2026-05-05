mode=parrp
run_test=true
run_parse=true

cd ../..
for t in probe nmshrs hol relbuf; do
  if $run_test == true; then
    TRACE_DIR=./traces sbt -mem 6000 "project chipyard" \
        "testOnly chipyard.ProtoTest -- -z Synthetic-$t-4 -oD" \
        2> traces/logs/synthetics/$t-4-$mode.log
    
    cd traces/mrt_data
    ./get_mrt.sh ../../test_run_dir/mem_req_times/ $t-4 -$mode
    cd ../..
  fi

  if $run_parse == true; then
    python3 traces/scripts/parse_results.py traces/logs/synthetics/$t-4-$mode.log \
        --csv traces/parsed/synthetics/$t-4-$mode.csv \
        --l1csv traces/parsed/synthetics/$t-4-l1-$mode.csv
  fi
done