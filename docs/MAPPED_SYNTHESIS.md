# Pinned IHP SG13G2 mapped synthesis

Hephaestus now has a standard-cell mapping evidence level above generic Yosys structure and below
physical design:

```text
standard_cell_mapped_area_estimate
```

It maps every verified matched backend through the same pinned IHP SG13G2 typical Liberty file,
structurally checks the mapped netlist, preserves all reports and netlists, and independently
recomputes the reported area from the cell histogram and Liberty `area` attributes.

## Pinned technology input

The technology configuration is:

```text
configs/technology/ihp_sg13g2_stdcell_typ_1p20V_25C.json
```

It pins:

- repository: `IHP-GmbH/IHP-Open-PDK`;
- commit: `22f2a25f1734796de3debbbf29cf697cbbc54081`;
- Liberty: `sg13g2_stdcell_typ_1p20V_25C.lib`;
- SHA-256: `7677a8918689f452e80405ad16a83e744709342574f2aedcc507c2758986b396`;
- file size: 1,720,231 bytes;
- nominal conditions: 1.2 V, 25 °C;
- Yosys/ABC package: `0.33-5build2`;
- Icarus package: `12.0-2build2`.

The downloaded Liberty must match the pinned byte count, digest, library name, voltage, and
temperature before mapping starts.

## Mapping flow

Each backend receives the same transparent Yosys script:

```text
read_liberty -lib
read_verilog -sv
hierarchy -check -top
proc
flatten
opt
techmap
opt
abc -liberty
clean -purge
check -assert
stat -liberty
write_verilog
write_json
```

`read_liberty -lib` provides Yosys with cell-port directions and black-box definitions. The flow
uses `check -assert`; undriven nets, conflicting drivers, and related structural problems therefore
fail the evidence build instead of remaining warnings.

## First mapped microcase

The regression case is the exact 4×6 integer core generated from `examples/tiny_weights.json`.
All backends use the same 48 input bits, 48 output bits, coefficient matrix, accumulator width,
and zero-cycle combinational contract.

| Backend | Mapped cells | Liberty-area sum |
|---|---:|---:|
| Shared Hephaestus DAG | **492** | **5350.6656** |
| Naive output-local shift/add | 574 | 6285.0816 |
| Explicit constant-multiplier source | 574 | 6248.7936 |

Relative to the naive shift/add baseline, the shared DAG uses:

- 82 fewer mapped cells, a 14.2857% reduction;
- 934.4160 fewer Liberty area units, a 14.8672% reduction.

Relative to the constant-multiplier source, the shared DAG uses:

- 82 fewer mapped cells, a 14.2857% reduction;
- 898.1280 fewer Liberty area units, a 14.3728% reduction.

This is a real standard-cell mapping result for the pinned microcase. It is not a claim that the
same percentage survives larger matrices, timing constraints, buffering, placement, routing, or
model-level scaling.

## Independent checks

For every backend the evidence builder requires:

1. the RTL digest to match the verified matched manifest;
2. the mapped input/output widths to match the arithmetic contract;
3. every remaining cell type to exist in the pinned Liberty;
4. `check -assert` to pass;
5. the Yosys `stat -liberty` area to equal the independently computed sum
   `Σ(cell count × Liberty cell area)`;
6. a second clean mapping to produce byte-identical mapped JSON, Verilog, and `stat` report;
7. normalized metrics to match across both runs.

The exact expected numbers are versioned in:

```text
benchmarks/reference/ihp_sg13g2_tiny_v1.json
```

CI fails when the compiler, backend, tool package, or flow changes those results without an explicit
reference update.

## Artifact bundle

A successful run retains:

```text
build/ihp-mapped/
├── source/
├── matched/
├── tooling/
├── input/technology.lib
└── evidence/
    ├── mapped_evidence.json
    ├── SUMMARY.md
    ├── source_matched_manifest.json
    ├── technology/
    │   ├── technology.json
    │   └── technology.lib
    ├── backends/<backend>/
    │   ├── input.sv
    │   ├── map.ys
    │   ├── mapped.v
    │   ├── mapped.json
    │   ├── mapped.stat.txt
    │   ├── yosys.stdout.txt
    │   └── yosys.stderr.txt
    └── repeatability/<backend>/
        └── ...
```

All preserved artifacts receive SHA-256 digests in `mapped_evidence.json`.

## Layered claim boundary

The mapping artifact itself may claim:

```json
{
  "standard_cell_mapping_performed": true,
  "mapped_netlist_structurally_checked": true,
  "post_mapping_library_area_estimated": true
}
```

It deliberately reports this field as false:

```json
{
  "mapped_gate_level_equivalence_verified": false
}
```

That is not a missing result hidden by documentation. Mapping and formal proof are separate evidence
operations. The subsequent
[Mapped standard-cell formal equivalence](MAPPED_FORMAL_EQUIVALENCE.md) layer consumes the
preserved mapped bundle, loads the Liberty Boolean functions, proves every post-ABC netlist against
an independent code-matrix reference, and emits a new manifest where mapped gate-level equivalence
is true.

Both layers must still report:

```json
{
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

The Liberty `area` values are library units used for a mapped-cell estimate. They are not placed die
area and are not labelled as square micrometres here. The next physical evidence level must add
matched timing constraints, placement, routing, extraction, and activity-based power before drawing
conclusions about PPA.
