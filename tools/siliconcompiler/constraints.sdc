# constraints.sdc
# 2ns = 0.5GHz
create_clock -name clock -period 8.0 [get_ports clock] 

# io delay
set_input_delay  0.1 -clock clock [all_inputs]
set_output_delay 0.1 -clock clock [all_outputs]