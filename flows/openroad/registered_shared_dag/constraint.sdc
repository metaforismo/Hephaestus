create_clock -name core_clock -period 4.0 [get_ports clk]

set data_inputs [all_inputs -no_clocks]
set_input_delay 0.20 -clock core_clock $data_inputs
set_output_delay 0.20 -clock core_clock [all_outputs]
set_clock_uncertainty 0.10 [get_clocks core_clock]

set_driving_cell -lib_cell sg13g2_buf_4 $data_inputs
set_load 0.01 [all_outputs]
