# Mapped standard-cell formal equivalence

Hephaestus has a formal evidence level for the netlists produced after standard-cell mapping:

```text
yosys_sat_standard_cell_mapped_combinational_equivalence
```

This layer answers a question that mapped cell counts and area reports cannot answer by themselves:

> Did technology mapping preserve the exact bounded integer function of the compiled matrix?

For every mapped backend, Hephaestus loads the preserved Liberty Boolean functions, reads the
post-ABC mapped Verilog, constructs an independent arithmetic reference directly from `codes.npy`,
and asks Yosys SAT to prove that the output mismatch bit is always zero.

## Evidence chain

The proof is built only after the preceding gates have succeeded:

```text
source tensor and quantized codes
              │
              ▼
verified matched RTL contract
              │
              ▼
exhaustive RTL equivalence
              │
              ▼
pinned IHP standard-cell mapping
              │
              ▼
structurally checked mapped netlist
              │
              ▼
exhaustive mapped-netlist equivalence
```

The mapped formal builder rejects an input bundle unless it records all of these claims:

```json
{
  "matched_integer_contract_verified": true,
  "standard_cell_mapping_performed": true,
  "mapped_netlist_structurally_checked": true
}
```

It also rechecks the SHA-256 digests of the preserved matched manifest, quantized code matrix,
Liberty file, technology configuration, and every mapped Verilog input before starting SAT.

## Independent reference

The reference module is regenerated from the exact integer coefficient matrix in `codes.npy`.
It deliberately does not read:

- the shared-adder DAG;
- the lowering plan;
- the emitted Hephaestus expressions;
- the mapped cell graph;
- the backend's internal topology.

Each output is reconstructed as a direct signed sum of coefficient-by-input products under the
same declared input and accumulator widths. This avoids proving one transformed copy of an
implementation against another transformed copy of the same implementation.

## Functional standard-cell model

The proof script begins with:

```text
read_liberty -ignore_miss_func technology.lib
```

Yosys therefore imports the Boolean functions of the cells used by the mapped netlist. The mapped
Verilog, independent reference, and mismatch miter are then processed together:

```text
read_verilog -sv
hierarchy -check -top
proc
flatten
opt
check -assert
sat -verify -set-def-inputs -prove mismatch 0
```

A proof is rejected when Yosys reports an unsupported imported cell or leaves a black-box cell in
the proof path. `hierarchy -check` and `check -assert` also make missing modules, malformed
connections, undriven logic, and conflicting drivers fail closed.

## First mapped proof case

The pinned regression case remains the exact 4×6 integer core generated from
`examples/tiny_weights.json`:

```text
input bus: 48 bits
output bus: 48 bits
latency: zero-cycle combinational
technology: IHP SG13G2 typical, 1.2 V, 25 °C
```

All three post-ABC mapped netlists are proved against the same independent reference:

| Backend | Mapped cells | Formal result |
|---|---:|:---:|
| Shared Hephaestus DAG | 492 | proved |
| Naive output-local shift/add | 574 | proved |
| Explicit constant-multiplier source | 574 | proved |

For each backend, the 48-bit symbolic input bus represents all:

\[
2^{48}
\]

defined bit patterns. SAT reasons about the Boolean formula symbolically; the workflow does not
iterate through those patterns one at a time.

## Negative control

A positive proof alone can be misleading when a miter is disconnected, a top module is wrong, or
an unsupported cell has been abstracted away. Hephaestus therefore performs a negative control in
the same evidence build.

For the mapped shared-DAG netlist, the harness introduces this data-dependent fault:

```text
output bit 0 ^= input bit 0
```

The negative run omits `-verify` and requires Yosys SAT to produce a counterexample. The evidence
build fails unless:

```json
{
  "counterexample_found": true,
  "proof_success": false,
  "unsupported_cell_error": false
}
```

This demonstrates that the input reaches the compared output and that the proof harness can detect
an actual semantic error.

## Command

After generating mapped evidence, run:

```bash
python -m hephaestus.mapped_formal build/ihp-mapped-formal/mapped \
  --codes build/ihp-mapped-formal/source/codes.npy \
  --out build/ihp-mapped-formal/formal \
  --max-input-bits 64 \
  --timeout 300
```

`--max-input-bits` is a deliberate resource and scope gate. A larger design is not silently called
formally verified when it exceeds the configured proof envelope.

## Preserved artifact bundle

The formal output contains:

```text
formal/
├── mapped_formal_evidence.json
├── SUMMARY.md
├── source_mapped_evidence.json
├── source_matched_manifest.json
├── source_codes.npy
├── reference.sv
├── technology/
│   ├── technology.json
│   └── technology.lib
├── backends/<backend>/
│   ├── dut.v
│   ├── reference.sv
│   ├── miter.sv
│   ├── proof.ys
│   ├── yosys.stdout.txt
│   └── yosys.stderr.txt
└── negative_control/
    └── ...
```

Every preserved input, generated harness, proof script, and log receives a SHA-256 digest in the
machine-readable manifest.

## Claim boundary

A successful mapped formal bundle may declare:

```json
{
  "standard_cell_mapping_performed": true,
  "mapped_netlist_structurally_checked": true,
  "mapped_gate_level_equivalence_verified": true,
  "exhaustive_combinational_equivalence_verified": true,
  "negative_control_counterexample_found": true
}
```

It must still report:

```json
{
  "sequential_equivalence_verified": false,
  "timing_constrained": false,
  "timing_analyzed": false,
  "power_estimated": false,
  "placement_performed": false,
  "routing_performed": false,
  "post_synthesis_ppa_measured": false,
  "post_layout_pex_verified": false,
  "silicon_verified": false
}
```

The proof covers the bounded four-state-free Boolean interpretation used by Yosys SAT for defined
inputs. It does not cover X/Z behavior, analog voltage behavior, delays, glitches, setup/hold,
variation, power, clocking, scan, physical routing, parasitics, or fabrication.
