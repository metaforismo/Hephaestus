# Research-only ORFS configuration for the registered shared-DAG microcase.
# This file is intentionally explicit so a successful smoke run can be promoted
# into a matched physical-evidence contract without silently inheriting defaults.

export PLATFORM = ihp-sg13g2
export DESIGN_NAME = hephaestus_registered_tile_shared_dag_registered
export DESIGN_NICKNAME = hephaestus_registered_shared_dag

# The workflow mounts the generated, digest-bound registered bundle here.
export VERILOG_FILES = $(HEPHAESTUS_REGISTERED_DIR)/shared_dag_core.sv \
                       $(HEPHAESTUS_REGISTERED_DIR)/shared_dag_registered.sv
export SDC_FILE = $(dir $(DESIGN_CONFIG))/constraint.sdc

export CLOCK_PORT = clk
export CLOCK_PERIOD = 4.0

# Fixed physical boundary for this probe. The permanent matched experiment must
# use the same dimensions and settings for all three backends.
export DIE_AREA = 0 0 240 240
export CORE_AREA = 20 20 220 220
export PLACE_DENSITY = 0.50

export MIN_ROUTING_LAYER = Metal2
export MAX_ROUTING_LAYER = Metal5

# Keep the first experiment deterministic and single-threaded at the OpenROAD
# command level. Tool-level provenance is captured by the workflow.
export OPENROAD_THREADS = 1
