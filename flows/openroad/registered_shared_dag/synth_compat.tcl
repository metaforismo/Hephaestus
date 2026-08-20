# Run the unmodified ORFS synthesis flow first.
source $::env(SCRIPTS_DIR)/synth.tcl

# OpenSTA's Verilog reader rejects the declaration form emitted by the current
# Yosys container for signed packed ports. Keep the original netlist and remove
# only declaration-level `signed` tokens under a fail-closed Python transform.
set netlist "$::env(RESULTS_DIR)/1_2_yosys.v"
set manifest "$::env(RESULTS_DIR)/1_2_yosys.opensta_compat.json"
exec -- \
  $::env(PYTHON_EXE) \
  /work/design/sanitize_yosys_netlist.py \
  $netlist \
  --manifest $manifest
