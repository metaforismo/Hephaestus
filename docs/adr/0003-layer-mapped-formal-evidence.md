# ADR 0003: Keep mapped synthesis and mapped formal proof as separate evidence layers

## Status

Accepted

## Context

A standard-cell mapping report can show that Yosys and ABC emitted cells from a pinned Liberty
library, and a structural check can reject malformed connectivity. Neither result proves that the
mapped netlist still implements the intended bounded integer function.

It would be tempting to set `mapped_gate_level_equivalence_verified` inside the mapping manifest
after a later workflow has run. That would make an earlier artifact appear to contain evidence it
did not generate and would blur provenance between synthesis and proof.

## Decision

Hephaestus keeps two immutable evidence layers:

1. `standard_cell_mapped_area_estimate` produces and validates the mapped netlists, cell histogram,
   Liberty-area sum, structural checks, and repeatability evidence. Its own gate-level-equivalence
   claim remains false.
2. `yosys_sat_standard_cell_mapped_combinational_equivalence` consumes that preserved bundle,
   verifies every digest, loads the Liberty Boolean functions, and proves the mapped Verilog against
   an independent reference regenerated directly from `codes.npy`.

The mapped formal layer must include a negative control that introduces a data-dependent output
fault and requires a counterexample.

## Consequences

- Evidence remains append-only and attributable to the operation that produced it.
- A mapping artifact can be inspected or reused without claiming a proof that was not run.
- A formal artifact records the exact mapped input and technology model it proved.
- Timing, power, placement, routing, X/Z, analog, post-layout, and silicon claims remain separate.
