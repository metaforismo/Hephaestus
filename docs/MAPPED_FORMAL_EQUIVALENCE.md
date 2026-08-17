# Formal equivalence of mapped standard-cell netlists

Hephaestus has a downstream evidence level that proves the Boolean behavior of each mapped
standard-cell netlist against an independent arithmetic reference:

```text
yosys_sat_standard_cell_mapped_equivalence
```

This sits after `standard_cell_mapped_area_estimate` and before timing-aware or physical-design
evidence.

## Evidence chain

The proof does not compare one backend with another. It reconstructs a separate reference directly
from the exact `codes.npy` matrix and declared integer contract:

```text
codes.npy ───────────────► independent arithmetic reference
                                  │
                                  ▼
mapped.v + pinned Liberty ─► mismatch miter ─► Yosys SAT
```

The reference uses the coefficient matrix, signed input width, and accumulator width. It does not
read the shared-adder DAG, compilation plan, source RTL expression tree, or mapped cell graph.

Before running SAT, the builder validates the complete provenance chain:

1. the mapped-evidence schema and mapping claims;
2. the preserved matched-manifest digest;
3. the `codes.npy` digest recorded by the matched manifest;
4. equality of the matched and mapped arithmetic contracts;
5. mapped-Verilog digests for every backend;
6. the pinned technology configuration and Liberty digests;
7. input/output widths and mapped cell histograms;
8. the presence of a functional Liberty model for every cell type used by each netlist.

Any mismatch fails before the proof engine is started.

## Liberty functional models

Mapping originally loads the Liberty with `read_liberty -lib`, which supplies black-box cell
interfaces to synthesis. Formal proof instead loads functional Boolean models with:

```text
read_liberty -ignore_miss_func technology.lib
```

The pinned IHP library contains 84 cell declarations, of which 74 expose at least one Boolean
`function`. All cell types used by the three mapped microcase netlists are inside that functional
subset. A mapped netlist that uses a cell without a functional model is rejected rather than treated
as an unconstrained black box.

## Proof flow

For each backend the preserved script is equivalent to:

```text
read_liberty -ignore_miss_func ../../technology/technology.lib
read_verilog -sv dut.v reference.sv miter.sv
hierarchy -check -top <miter>
proc
flatten
opt
check -assert
sat -verify -set-def-inputs -prove mismatch 0 -show-inputs -show-outputs
```

A positive result must contain:

```text
SAT proof finished - no model found: SUCCESS!
```

The evidence builder also rejects missing-cell, unreadable-input, and black-box import failures even
when the process return code is zero.

## Verified microcase scope

The pinned 4×6 case has:

```text
input bus: 48 bits
output bus: 48 bits
domain: quantized integer core before row scaling
latency: combinational, zero cycles
```

The proof is symbolic over every defined 48-bit input assignment. This corresponds to all
\(2^{48}\) possible bit patterns; Yosys does not enumerate them one by one.

The proof covers:

- the 492-cell shared Hephaestus DAG netlist;
- the 574-cell naive output-local shift/add netlist;
- the 574-cell explicit constant-multiplier netlist.

Each is proved against the same independent code-matrix reference, not merely against the
pre-mapping RTL.

## Negative control

A successful proof is insufficient unless the harness can also detect a real semantic difference.
The evidence builder therefore creates a fourth miter in which output bit zero is XORed with input
bit zero.

That data-dependent fault must produce:

```text
SAT proof finished - model found: FAIL!
```

The evidence build fails if the negative control is accidentally proved, if no counterexample is
found, or if the proof harness is disconnected.

## Command

After producing a mapped evidence bundle:

```bash
python -m hephaestus.mapped_formal build/ihp-mapped-formal/mapped \
  --codes build/ihp-mapped-formal/source/codes.npy \
  --out build/ihp-mapped-formal/proof \
  --max-input-bits 64 \
  --timeout 300
```

`--max-input-bits` is an explicit resource guard. The builder refuses wider proof obligations rather
than silently changing the proof method or reducing coverage.

## Artifact bundle

A successful proof retains:

```text
proof/
├── mapped_formal_evidence.json
├── SUMMARY.md
├── source_mapped_evidence.json
├── source_matched_manifest.json
├── source_codes.npy
├── reference.sv
├── technology/
│   ├── technology.json
│   └── technology.lib
└── proofs/
    ├── shared_dag/
    ├── naive_shift_add/
    ├── constant_multipliers/
    └── negative_control/
```

Every proof directory contains the mapped DUT, independent reference, miter, exact Yosys script,
stdout, stderr, and SHA-256 provenance.

## Claims enabled by this evidence

The downstream manifest may report:

```json
{
  "standard_cell_mapping_performed": true,
  "mapped_netlist_structurally_checked": true,
  "liberty_functional_models_verified": true,
  "mapped_gate_level_equivalence_verified": true,
  "exhaustive_combinational_equivalence_verified": true,
  "negative_control_counterexample_found": true,
  "post_mapping_library_area_estimated": true
}
```

The original mapped-area manifest remains immutable and continues to report
`mapped_gate_level_equivalence_verified: false`; the proof manifest references and extends that
evidence rather than rewriting it after the fact.

## Claim boundary

This is a two-state, defined-input, zero-delay combinational proof. It does not establish:

- X/Z or unknown-state equivalence;
- sequential, pipelined, reset, scan, or clock equivalence;
- SDF or timing-annotated gate-level behavior;
- setup/hold correctness or static timing closure;
- placement, routing, parasitics, crosstalk, IR drop, or electromigration;
- leakage or activity-based dynamic power;
- post-synthesis PPA measurement or physical die area;
- DRC, LVS, PEX, package, board, or fabricated-silicon behavior.

The next evidence level should add one matched timing contract and a reproducible OpenROAD flow,
while preserving this functional proof as a prerequisite.
