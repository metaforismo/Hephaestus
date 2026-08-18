# Formally bound OpenSTA pre-layout timing evidence

This evidence layer measures the exact standard-cell netlists whose Boolean behavior was already
proved against the independent integer reference:

```text
verified integer contract
  → matched RTL backends
  → IHP SG13G2 ABC area-delay sweep
  → exhaustive SAT proof of every distinct mapped netlist
  → pinned OpenSTA pre-layout timing
  → digest-level binding between each timing result and its proof
```

The timing stage does not inherit equivalence from a backend name or sweep label. Every result must
carry the same mapped-Verilog SHA-256 digest as a successful positive SAT proof. The formal harness
must also retain a successful data-dependent negative control.

## Qualified microcase

The pinned regression case is the exact 4×6 integer core derived from
`examples/tiny_weights.json`:

```text
input bus: 48 bits
output bus: 48 bits
latency: zero-cycle combinational
source sweep: 18 labeled ABC runs
unique mapped netlists proved: 6
formally proved netlists timed: 6
```

The six timed designs are the two observed ABC Pareto mappings for each matched backend:
`unconstrained` and `d4000ps`.

## Captured OpenSTA toolchain

The workflow builds the official `parallaxsw/OpenSTA` source at:

```text
2b751f0e8196b05ef4ed8246b7e27c63c967ec6d
```

It also pins and verifies CUDD 3.0.0, captures the Flex header digest, package manifest, dynamic
library manifest, OpenSTA binary, build logs, and the exact IHP SG13G2 Liberty file. The qualifying
run identified OpenSTA as `3.1.0`.

A rebuild of the same source is not claimed to produce a byte-identical OpenSTA binary on every
runner. Each evidence bundle therefore preserves and hashes the exact binary it used. Repeatability
means that two executions with that captured binary produced byte-identical stdout and stderr plus
identical normalized timing values for every netlist.

## Common timing contract

Every timed netlist uses:

```text
technology:       IHP SG13G2 typical, 1.2 V, 25 °C
virtual period:   4.0 ns
input delay:      0.0 ns
output delay:     0.0 ns
input driver:     sg13g2_buf_4
output load:      0.01 pF per primary output
parasitics:       none
path type:        maximum delay
```

The 4 ns virtual period is a reporting frame for slack calculation. It is not a product clock claim
or evidence of sequential timing closure.

## Pinned result

| Backend | ABC mapping | Liberty area | ABC delay | OpenSTA data delay | Worst slack |
|---|---|---:|---:|---:|---:|
| Shared DAG | `unconstrained` | **5439.5712** | **2029.49 ps** | **2.187032223 ns** | 1.812967777 ns |
| Naive shift/add | `unconstrained` | 6339.5136 | 2183.17 ps | 2.356311440 ns | 1.643688560 ns |
| Constant multipliers | `unconstrained` | 6334.0704 | 2270.49 ps | 2.390366912 ns | 1.609633088 ns |
| Shared DAG | `d4000ps` | **5386.9536** | **2077.58 ps** | **2.240758538 ns** | 1.759241462 ns |
| Naive shift/add | `d4000ps` | 6306.8544 | 2217.09 ps | 2.400178790 ns | 1.599821210 ns |
| Constant multipliers | `d4000ps` | 6277.8240 | 2285.89 ps | 2.400267601 ns | 1.599732399 ns |

Under this exact pre-layout contract, the shared DAG has the lowest OpenSTA data delay at both
Pareto mappings. At `unconstrained`, its delay is 7.1841% lower than naive shift/add and 8.5064%
lower than the constant-multiplier source. At `d4000ps`, the reductions are 6.6420% and 6.6455%.

These values characterize one small regression case. They are not a claim that the same ordering
must hold for every matrix, width, placement, routing topology, or physical corner.

## Fail-closed binding

`python -m hephaestus.opensta_binding` rejects:

- malformed or unverified formal and timing manifests;
- an absent or ineffective formal negative control;
- different source ABC evidence or Liberty digests;
- unsafe or malformed tool and netlist digests;
- timed backend/label pairs outside the formally proved Pareto set;
- missing, duplicate, or reused mapped-netlist digests;
- a timing result whose digest differs from its positive proof;
- timing assumptions that drift from the common contract;
- failed or non-repeatable OpenSTA executions;
- inconsistent period, slack, delay, ABC area, or ABC delay values.

The final binding manifest is:

```text
hephaestus.opensta-formal-binding.v1
```

and its evidence level is:

```text
opensta_pre_layout_timing_of_formally_proved_abc_netlists
```

## Production timing interface

The preparation, Tcl generation, OpenSTA execution, report parsing, repeatability comparison, and
manifest normalization live in one package module:

```text
src/hephaestus/opensta_timing.py
```

Prepare the selected mapped netlists with:

```bash
python -m hephaestus.opensta_timing prepare \
  build/opensta-evidence/area-delay \
  --out build/opensta-evidence/timing \
  --period-ns 4.0 \
  --input-delay-ns 0.0 \
  --output-delay-ns 0.0 \
  --driving-cell sg13g2_buf_4 \
  --output-load-pf 0.01 \
  --labels unconstrained d4000ps
```

Run the prepared analyses twice with the captured binary:

```bash
python -m hephaestus.opensta_timing run \
  build/opensta-evidence/timing \
  --sta build/opensta-evidence/tooling/opensta.bin \
  --tool-metadata build/opensta-evidence/tooling/tool.json \
  --attempts 2 \
  --timeout 300
```

The older temporary wrapper and base scripts were removed after their behavior was consolidated and
tested. The existing schema identifiers `hephaestus.opensta-sdc-prepared.v1` and
`hephaestus.opensta-sdc-probe.v1` remain unchanged deliberately, so previously published evidence
bundles and the formal binder stay compatible. Their names identify an artifact contract, not a
current source-file layout or an unqualified research claim.

## Reproduction

The complete flow is exercised by:

```text
.github/workflows/opensta-timing-evidence.yml
```

The permanent workflow builds the pinned toolchain, regenerates the matched sweep, proves all six
distinct mapped netlists, runs each OpenSTA analysis twice, binds the timing results to the exact
proof digests, validates the regression reference, and uploads a self-contained artifact.

The qualifying run was `32144004895` on head
`ab66a895dc22eae2be0fef22dcd818e6ce58ac6d`. Its uploaded ZIP has SHA-256:

```text
29db05b84654059944df43c5f2c371fecda6a0cf41fe0228a4a97962b03ac44a
```

## Claim boundary

This layer can claim:

- pre-layout OpenSTA timing analysis under one explicit boundary contract;
- timing of six exact mapped netlists with successful exhaustive combinational proofs;
- a successful formal negative control;
- two byte-identical timing attempts per netlist with one captured OpenSTA binary;
- finite maximum-path data delays and no negative slack under the 4 ns reporting frame.

It cannot claim:

- sign-off STA;
- clocked or sequential timing closure;
- placed or routed timing;
- annotated wire or extracted RC delay;
- variation, crosstalk, IR-drop, or electromigration closure;
- dynamic or leakage power;
- post-layout PPA;
- DRC, LVS, PEX, or fabricated-silicon behavior.

The next physical evidence layer is matched placement, routing, extraction, and activity-based power
for the same formally proved netlists on the open IHP SG13G2 flow.
