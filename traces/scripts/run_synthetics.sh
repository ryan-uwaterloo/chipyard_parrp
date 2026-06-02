mode=ctrl
run_test=false
run_parse=false
run_8=true
parse_8=true

cd ../..
# for t in nmshrs; do # -> stock # mshrs
# for t in probe hol relbuf inter-iso inter-int; do # -> 20 mshrs
for t in mempressure; do # -> mem_BW = 100, mem_Lat=10
# for t in probe; do
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

for t in hol; do # 40 mshrs, mem_BW=10, mem_Lat =100
  if $run_8 == true; then
    TRACE_DIR=./traces sbt -mem 6000 "project chipyard" \
        "testOnly chipyard.ProtoTest -- -z Synthetic-$t-8 -oD" \
        2> traces/logs/synthetics/$t-8-$mode.log
    
    cd traces/mrt_data
    ./get_mrt.sh ../../test_run_dir/mem_req_times/ $t-8 -$mode
    cd ../..
  fi

  if $parse_8 == true; then
    python3 traces/scripts/parse_results.py traces/logs/synthetics/$t-8-$mode.log \
        --csv traces/parsed/synthetics/$t-8-$mode.csv \
        --l1csv traces/parsed/synthetics/$t-8-l1-$mode.csv
  fi
done