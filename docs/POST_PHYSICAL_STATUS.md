# Post-physical equivalence status

Hephaestus has qualified matched IHP SG13G2 placement-and-routing evidence for the registered 4×6 regression microcase. The physical bundle contains two repeatable attempts for each of:

```text
shared_dag
naive_shift_add
constant_multipliers
```

The physical layer binds registered source digests, final routed Verilog, GDS, DEF, OpenDB, SPEF, tool provenance, the common floorplan/timing boundary, and selected stable metrics.

## What is not yet qualified

A post-physical research harness was merged while investigating routed sequential equivalence. Its historical full routed-netlist bounded SAT execution timed out before it produced a final evidence manifest. The repository therefore does **not** currently treat that harness as a permanent proof layer.

The following claims remain false:

```json
{
  "post_physical_equivalence_verified": false,
  "comparative_ppa_claim_enabled": false,
  "four_state_semantics_verified": false,
  "timing_annotated_functional_semantics_verified": false,
  "drc_clean": false,
  "lvs_clean": false,
  "power_estimated_with_activity": false,
  "post_layout_pex_verified": false,
  "foundry_signoff_complete": false,
  "silicon_verified": false
}
```

## Promotion requirements

A permanent post-physical layer must be rebuilt as a normal package module and same-run workflow stage. It must:

1. consume the prepared registered source, both physical attempts per backend, and the bound physical evidence from one workflow run;
2. verify every source, routed-netlist, physical-manifest, functional-cell-model, proof-script, and log digest;
3. prove the declared clock-edge transaction behavior for all three routed netlists with a method that completes reliably in CI;
4. require independent data, valid-latency, and reset-state negative controls;
5. preserve the asynchronous-reset, timing, X/Z, analog, DRC/LVS, PEX, power, and silicon boundaries;
6. pin only stable regression invariants after a real qualifying run;
7. contain no historical run IDs, one-shot writers, research-only workflows, or expiring artifact dependencies.

Until those conditions are met, physical metrics may be inspected as observations, but they must not be presented as a functionally qualified comparative PPA result.
