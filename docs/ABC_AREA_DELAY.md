# Technology-aware ABC area-delay evidence

Hephaestus now has a bounded evidence level between unconstrained standard-cell mapping and physical
static timing analysis:

```text
abc_liberty_area_delay_estimate
```

It runs the same verified integer core through the same pinned IHP SG13G2 Liberty under one
versioned primary-input driver and primary-output load, sweeps declared ABC delay targets, and
preserves the achieved mapping, area, and `stime -p` delay for every backend.

This is useful evidence, but it is deliberately not called sign-off STA.

## Why this is a separate evidence level

The pinned tool is Yosys 0.33. Its `abc` implementation selects a distinct Liberty-with-constraints
flow when `-constr` is present. That flow ends with:

```text
buffer
upsize {D}
dnsize {D}
stime -p
```

`-D` is a requested delay target in picoseconds. It is not the measured result. The measured value
is the delay printed afterward by `stime -p`, and the evidence records target attainment by
comparing the two.

Yosys's built-in `sta` pass is not used as a substitute Liberty analyzer here. In this pinned
version, `sta` consumes timing arcs represented as `$specify` cells, while `read_liberty -lib`
creates empty black-box modules. Running `sta` after that import would therefore not constitute
Liberty-table STA. A later OpenSTA/OpenROAD evidence level must provide explicit timing constraints,
real timing-library analysis, and physical context.

## Pinned inputs

The technology input remains:

```text
IHP-Open-PDK commit:
22f2a25f1734796de3debbbf29cf697cbbc54081

Liberty:
sg13g2_stdcell_typ_1p20V_25C.lib

SHA-256:
7677a8918689f452e80405ad16a83e744709342574f2aedcc507c2758986b396

corner:
typical, 1.2 V, 25 °C
```

The area-delay configuration is:

```text
configs/evidence/ihp_sg13g2_abc_area_delay_v1.json
```

It declares:

```text
primary-input driving cell: sg13g2_buf_4
primary-output load:        10.0 fF per output
delay targets:              none, 1000, 2000, 4000, 8000, 16000 ps
```

These assumptions are not universal properties of the circuit. They are part of the evidence
contract and must be changed through a new versioned configuration.

## Command

After producing a verified matched bundle:

```bash
python -m hephaestus.abc_timing build/abc-area-delay/matched \
  --technology configs/technology/ihp_sg13g2_stdcell_typ_1p20V_25C.json \
  --liberty build/abc-area-delay/input/technology.lib \
  --config configs/evidence/ihp_sg13g2_abc_area_delay_v1.json \
  --out build/abc-area-delay/evidence \
  --verify-repeatability \
  --timeout 300
```

Every backend and target receives an isolated script equivalent to:

```text
read_liberty -lib
read_verilog -sv
hierarchy -check -top
proc
flatten
opt
techmap
opt
abc -liberty -constr [-D target_ps]
clean -purge
check -assert
stat -liberty
write_verilog
write_json
```

## First verified 4×6 result

The exact microcase has 48 input bits, 48 output bits, a zero-cycle combinational contract, and the
same quantized integer coefficient matrix for all backends.

### Unconstrained ABC point

| Backend | Cells | Liberty area | ABC delay | Area-delay product |
|---|---:|---:|---:|---:|
| Shared Hephaestus DAG | **497** | **5439.5712** | **2029.49 ps** | **11,039,555.35** |
| Naive output-local shift/add | 577 | 6339.5136 | 2183.17 ps | 13,840,235.91 |
| Explicit constant-multiplier source | 578 | 6334.0704 | 2270.49 ps | 14,381,443.50 |

Relative to naive shift/add, the shared DAG reduces:

- Liberty area by **14.1958%**;
- ABC delay by **7.0393%**;
- area-delay product by **20.2358%**.

Relative to the constant-multiplier source, it reduces:

- Liberty area by **14.1220%**;
- ABC delay by **10.6144%**;
- area-delay product by **23.2375%**.

### Relaxed 4000 ps target

| Backend | Cells | Liberty area | ABC delay | Target met |
|---|---:|---:|---:|:---:|
| Shared Hephaestus DAG | **497** | **5386.9536** | **2077.58 ps** | yes |
| Naive output-local shift/add | 577 | 6306.8544 | 2217.09 ps | yes |
| Explicit constant-multiplier source | 578 | 6277.8240 | 2285.89 ps | yes |

The shared DAG remains both smaller and faster. Relative to naive shift/add, it reduces area by
**14.5857%**, delay by **6.2925%**, and area-delay product by **19.9604%**. Relative to the
constant-multiplier source, the reductions are **14.1908%**, **9.1129%**, and **22.0104%**.

## The target is not the achieved delay

The 1000 ps and 2000 ps requests are not met:

```text
shared DAG achieved delay:            2029.49 ps
naive shift/add achieved delay:       2183.17 ps
constant-multiplier achieved delay:   2270.49 ps
```

That is expected and important. The evidence stores both:

```text
target_picoseconds
critical_path_delay_picoseconds
target_met
target_margin_picoseconds
```

A tight `-D` option can guide mapping without making the resulting circuit satisfy that target. CI
must never infer a frequency from the requested value.

## Pareto points

The six requested targets collapse to two distinct mapped points for each backend:

```text
unconstrained
4000 ps
```

The 1000 ps and 2000 ps runs reproduce the unconstrained point. The 8000 ps and 16000 ps runs
reproduce the 4000 ps point. Both distinct points are Pareto-optimal: one has lower delay, while the
other has lower area.

The evidence builder removes duplicate area-delay coordinates before computing the Pareto front,
while preserving every raw target run and its artifacts.

## Independent checks

For every backend and target the builder requires:

1. the matched manifest and RTL digest to be valid;
2. the mapped input and output widths to equal the arithmetic contract;
3. every mapped cell type to exist in the exact pinned Liberty;
4. `check -assert` to pass;
5. exactly one ABC library summary and exactly one `stime -p` record;
6. ABC gate count to equal mapped JSON cell count;
7. ABC area to agree with `stat -liberty` within its printed precision;
8. Yosys area to equal the independent sum `Σ(cell count × Liberty cell area)`;
9. delay to be a finite positive value reported in picoseconds;
10. a second clean run to produce byte-identical mapped JSON, Verilog, and area reports, plus
    identical normalized area-delay metrics.

Exact expected values are pinned in:

```text
benchmarks/reference/ihp_sg13g2_abc_area_delay_tiny_v1.json
```

## Artifact bundle

A successful run preserves:

```text
build/abc-area-delay/evidence/
├── abc_area_delay_evidence.json
├── SUMMARY.md
├── source_matched_manifest.json
├── technology/
│   ├── technology.json
│   └── technology.lib
├── configuration/
│   └── area_delay.json
├── constraints/
│   └── abc.constr
├── runs/<backend>/<target>/
│   ├── input.sv
│   ├── map.ys
│   ├── mapped.v
│   ├── mapped.json
│   ├── mapped.stat.txt
│   ├── yosys.stdout.txt
│   └── yosys.stderr.txt
└── repeatability/<backend>/<target>/
    └── ...
```

## Claim boundary

This evidence may claim:

```json
{
  "technology_aware_abc_mapping_performed": true,
  "declared_input_driver_model_used": true,
  "declared_output_load_used": true,
  "abc_internal_timing_estimated": true,
  "abc_delay_targets_swept": true,
  "target_attainment_evaluated": true,
  "post_mapping_library_area_estimated": true,
  "area_delay_product_computed": true
}
```

It must still report:

```json
{
  "mapped_gate_level_equivalence_verified": false,
  "signoff_sta_performed": false,
  "sdc_timing_analyzed": false,
  "timing_closed": false,
  "power_estimated": false,
  "placement_performed": false,
  "routing_performed": false,
  "post_synthesis_ppa_measured": false,
  "post_layout_pex_verified": false,
  "silicon_verified": false
}
```

The next timing level should use OpenSTA or OpenROAD with a versioned SDC, explicit input delays,
output loads, transition assumptions, operating corner, and later placed/routed parasitics. The ABC
result is a compiler-feedback signal and architecture-ranking experiment, not tapeout sign-off.
