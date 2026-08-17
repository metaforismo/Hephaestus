# OpenROAD backend status

This directory is intentionally a scaffold, not a claim of a completed RTL-to-GDS flow.

The planned backend will accept:

- generated RTL and top module;
- a public or privately mounted PDK target;
- clock, I/O, utilization, and floorplan constraints;
- pinned Yosys/OpenROAD/KLayout revisions;
- matched baseline designs.

It must emit raw synthesis, placement, routing, timing, congestion, and power evidence plus a
machine-readable summary. PDK files and NDA-gated collateral must be supplied outside the repo.

The first supported public target is expected to use the IHP SG13G2 open PDK after the clocked tile
interface and matched baselines are implemented.
