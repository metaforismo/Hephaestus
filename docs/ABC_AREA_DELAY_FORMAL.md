# Formal equivalence of the ABC area-delay sweep

The ABC area-delay evidence deliberately stops before functional proof. It records mapped netlists,
technology-aware delay estimates, area, target attainment, and repeatability, while leaving:

```json
{
  "mapped_gate_level_equivalence_verified": false
}
```

A separate downstream artifact now consumes that immutable source bundle and proves every distinct
mapped netlist against an independent integer reference.

## Evidence level

```text
yosys_sat_abc_area_delay_mapped_equivalence
```

The source evidence remains:

```text
abc_liberty_area_delay_estimate
```

The two artifacts are not merged because mapping/timing estimation and Boolean equivalence are
different operations with different failure modes.

## Scope

The pinned microcase uses:

```text
matrix:       4 × 6
input bus:    48 bits
output bus:   48 bits
latency:      zero-cycle combinational
technology:   IHP SG13G2 typical, 1.2 V, 25 °C
input driver: sg13g2_buf_4
output load:  10 fF per primary output
```

The ABC sweep has six labels for each of the three backends:

```text
unconstrained
d1000ps
d2000ps
d4000ps
d8000ps
d16000ps
```

That is 18 source runs. Some targets produce byte-identical mapped Verilog. The proof builder groups
runs only by the SHA-256 digest of the mapped Verilog and proves each distinct digest once.

For the pinned case:

| Backend | Sweep runs | Distinct mapped netlists | Representatives |
|---|---:|---:|---|
| Shared DAG | 6 | 2 | `unconstrained`, `d4000ps` |
| Naive shift/add | 6 | 2 | `unconstrained`, `d4000ps` |
| Constant-multiplier source | 6 | 2 | `unconstrained`, `d4000ps` |

Therefore six positive SAT proofs cover all 18 sweep labels. A run may reuse a proof only when its
mapped-Verilog digest is byte-identical to the representative's digest. Equal area or equal delay is
not enough.

## Independent reference

The proof reference is regenerated directly from the quantized integer matrix in `codes.npy`.

It does not consume:

- the shared-adder DAG;
- the compiler lowering plan;
- source RTL expressions;
- ABC topology;
- area or delay values;
- Pareto decisions.

The reference and every mapped netlist receive the same declared signed input and output widths.

## Proof flow

Each representative uses:

```text
read_liberty -ignore_miss_func technology.lib
read_verilog -sv dut.v reference.sv miter.sv
hierarchy -check -top <miter>
proc
flatten
opt
check -assert
sat -verify -set-def-inputs -prove mismatch 0
```

Before SAT, the builder verifies:

1. the source ABC evidence schema and evidence level;
2. all required source claims and the source artifact's unproved equivalence boundary;
3. matched-manifest and `codes.npy` digests;
4. contract equality, zero-cycle combinational scope, and safe accumulator width;
5. technology configuration and Liberty digests;
6. source ABC configuration and constraints digests;
7. every mapped-Verilog digest;
8. mapped input/output widths;
9. cell-count and histogram consistency;
10. a functional Liberty model for every used standard cell;
11. passing structural and repeatability checks for every sweep run;
12. the configured formal input-width ceiling.

## Negative control

The shared-DAG representative is copied into a synthetic miter with:

```text
output bit 0 ^= input bit 0
```

The evidence build fails unless Yosys finds a counterexample. It also fails if the log reports an
unsupported imported cell or a black-box path.

This guards against disconnected outputs, an incorrectly wired reference, and a proof parser that
treats every solver exit as success.

## Artifact structure

```text
formal/
├── abc_area_delay_formal_evidence.json
├── SUMMARY.md
├── source_abc_area_delay_evidence.json
├── source_matched_manifest.json
├── source_codes.npy
├── reference.sv
├── technology/
│   ├── technology.json
│   └── technology.lib
├── configuration/
│   ├── area_delay.json
│   └── abc.constr
└── proofs/
    ├── shared_dag__unconstrained/
    ├── shared_dag__d4000ps/
    ├── naive_shift_add__unconstrained/
    ├── naive_shift_add__d4000ps/
    ├── constant_multipliers__unconstrained/
    ├── constant_multipliers__d4000ps/
    └── negative_control/
```

Every proof directory preserves the mapped DUT, independent reference, miter, exact Yosys script,
stdout, stderr, and SHA-256 provenance.

## Command

After generating the source and ABC area-delay evidence:

```bash
python -m hephaestus.abc_timing_formal \
  build/ihp-abc-formal/area-delay \
  --codes build/ihp-abc-formal/source/codes.npy \
  --out build/ihp-abc-formal/formal \
  --max-input-bits 64 \
  --timeout 300
```

## Claim boundary

A successful artifact may claim:

```json
{
  "all_abc_sweep_mapped_netlists_equivalent": true,
  "all_pareto_mapped_netlists_equivalent": true,
  "mapped_gate_level_equivalence_verified": true,
  "exhaustive_combinational_equivalence_verified": true,
  "negative_control_counterexample_found": true
}
```

It does not turn the ABC delay into sign-off timing. The following remain false:

```json
{
  "sequential_equivalence_verified": false,
  "four_state_equivalence_verified": false,
  "signoff_sta_performed": false,
  "sdc_timing_analyzed": false,
  "timing_closed": false,
  "power_estimated": false,
  "placement_performed": false,
  "routing_performed": false,
  "post_layout_pex_verified": false,
  "silicon_verified": false
}
```

The next timing layer should use explicit SDC through OpenSTA/OpenROAD, followed by matched placement
and routing. The OpenROAD project recommends OpenROAD Flow Scripts as its native prototyping and
tapeout flow, and ORFS includes an open `ihp-sg13g2` platform. Exact tool and platform revisions
must be pinned before those results become regression evidence.
