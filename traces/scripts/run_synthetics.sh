mode=ctrl
run_test=true
run_parse=true
run_6=true
parse_6=true
run_8=true
parse_8=true

cd ../..
# mem_BW = 10, mem_Lat=100
# for t in mshrs-gen probe-gen hol-gen inter-int-gen inter-iso-gen; do # -> stock # mshrs
# for t in nmshrs probe hol relbuf inter-iso inter-int; do # -> 20 mshrs
for t in hol-gen; do #
# for t in probe-gen hol-gen inter-int-gen inter-iso-gen; do
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

# 6 cores
# for t in hol relbuf-gen; do # 30 mshrs, mem_BW=10, mem_Lat =100
for t in hol-gen; do # 30 mshrs, mem_BW=10, mem_Lat =100
  if $run_6 == true; then
    TRACE_DIR=./traces sbt -mem 6000 "project chipyard" \
        "testOnly chipyard.ProtoTest -- -z Synthetic-$t-6 -oD" \
        2> traces/logs/synthetics/$t-6-$mode.log
    
    cd traces/mrt_data
    ./get_mrt.sh ../../test_run_dir/mem_req_times/ $t-6 -$mode
    cd ../..
  fi

  if $parse_8 == true; then
    python3 traces/scripts/parse_results.py traces/logs/synthetics/$t-6-$mode.log \
        --csv traces/parsed/synthetics/$t-6-$mode.csv \
        --l1csv traces/parsed/synthetics/$t-6-l1-$mode.csv
  fi
done

# 8 cores
# for t in hol relbuf-gen; do # 40 mshrs, mem_BW=10, mem_Lat =100
for t in hol-gen; do # 40 mshrs, mem_BW=10, mem_Lat =100
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