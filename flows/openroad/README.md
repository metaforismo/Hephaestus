# OpenROAD backend status

This directory remains a scaffold, not a claim of a completed RTL-to-GDS flow. The prerequisite
clocked boundary is now implemented separately in `hephaestus.registered`: the three matched
combinational cores receive identical input/output registers, valid propagation, reset behavior,
and one-cycle streaming latency, with a pinned simulation regression and explicit claim boundary.

The first OpenROAD backend will consume that registered evidence bundle rather than inventing a new
interface inside the physical flow. It must accept:

- the exact registered manifest and source-core proof bindings;
- generated core and wrapper RTL plus top-module names;
- a public or privately mounted PDK target;
- one common clock, I/O, utilization, and floorplan contract;
- pinned Yosys/OpenROAD/KLayout revisions;
- all three matched backend designs.

The initial physical experiment should keep the comparison narrow:

```text
same matrix
same arithmetic widths
same register boundary
same clock period
same utilization target
same die/core dimensions
same pin layers
same placement and routing settings
```

It must emit raw synthesis, floorplan, placement, routing, timing, congestion, extraction, and
power evidence plus a machine-readable summary. Every generated netlist must stay digest-bound to
its registered source, and physical evidence must not inherit correctness merely from a backend
name. PDK files and NDA-gated collateral must remain outside the repository.

The preferred first public target is IHP SG13G2. The next implementation step is a pinned
single-backend smoke flow for the registered shared DAG, followed by the two matched baselines under
the exact same physical contract. DRC/LVS, extracted RC, activity-based power, and post-layout
equivalence remain separate later gates; successful placement or routing alone will not be reported
as foundry sign-off.
