# Registered shared-DAG IHP SG13G2 smoke result

The research probe completed a full ORFS RTL-to-GDS flow for the digest-bound registered `shared_dag` tile.

## Qualified execution

```text
GitHub head:     1d77d85c18d43934bcb247d113a69a18c78575eb
Workflow run:    32200874370
Artifact ID:     9347528965
Artifact ZIP:    f7ecb1ba62a7cf6390cf0277b665f94079cf3ce3d3348ce56732c1b31241b863
ORFS return code: 0
```

The immutable container identity captured by the run is:

```text
openroad/orfs@sha256:73bd87efa06758865277f347fbc6b932642d8ab21a5430c5ce5480aaa60c27d0
```

Tool versions reported by that image:

```text
OpenROAD 26Q3-1305-gf552262465
Yosys 0.68+post
KLayout 0.30.7
```

The image does not retain a readable ORFS Git checkout revision, so the permanent evidence must treat the repository digest and captured tool banners as the executable authority rather than claiming a source-commit identity that was not observable.

## Physical contract

```text
platform:              ihp-sg13g2
backend:               shared_dag
clock:                 core_clock on clk
period:                4.0 ns
input/output delay:    0.2 ns
clock uncertainty:     0.1 ns
input driver:          sg13g2_buf_4
output load:           0.01 pF
die:                   0 0 240 240 um
core:                  20 20 220 220 um
placement density:     0.50
routing layers:        Metal2 through Metal5
ORFS NUM_CORES:        1
transactional LEC:     disabled
```

ORFS's bundled Kepler Formal executable was disabled for this probe because it terminated with `SIGILL` on the GitHub runner after CTS repair. This does not promote a functional-equivalence claim: post-physical sequential equivalence remains a separate mandatory gate.

The current Yosys image emits declaration-level `signed` tokens that this OpenROAD/OpenSTA Verilog reader rejects. The flow therefore preserves the original synthesized netlist and applies a fail-closed transform that removes exactly four declaration-only tokens, records both SHA-256 digests, preserves line count, and rejects any other use of `signed`. The transform itself does not claim functional equivalence.

## Final outputs

```text
6_final.gds   1,379,218 bytes  c138c951498695f61cf5dda2aeae46f86599c0fc5052364ddb0b0ce5814f550e
6_final.def   1,184,080 bytes  7431f1ed67a9f316f07d98a8bf57f61acd271be3c95885a71ab3f8ca36306fcf
6_final.odb   2,522,607 bytes  77bc19d1a7f47cd4cc739623ff4939060df20b8fc945f50429cfd55de5b9b350
6_final.v       112,890 bytes  9a8b0bf370d5a7013dcac059361b9fa36934eefb454b5c661c269c64c949b4fa
6_final.spef    845,857 bytes  c48abb9eb4d9479a4f99a488f4ba246f6589b88f8800eb8b4dc5070ea5d2a16a
6_final.sdc      19,227 bytes  7920a145c5bb650d328165b827681ddd9be4168bf08ed509353c39cd73793b74
```

The GDS timestamp-normalized signature is:

```text
0e0b995933927c2541de7631bd1b6723f261ef7fbdce0c44623f2165b8e3e5f8
```

Only the 60 `BGNLIB`/`BGNSTR` timestamp records are normalized. Geometry and all other record payloads remain part of the signature.

## Final observed metrics

```text
die area:                  57,600 um^2
core area:                 39,249.1 um^2
non-fill instance area:    16,180.8 um^2
non-fill utilization:      41.226%
standard-cell instances:   1,211
sequential cells:          96
clock buffers:             17
timing-repair buffers:     196
fill cells:                2,530
setup slack:               +0.643739 ns
hold slack:                +0.133453 ns
setup TNS:                 0
hold TNS:                  0
reported fmax:              297.951 MHz
setup skew:                0.107097 ns
hold skew:                 0.110776 ns
flow errors:               0
flow warnings:             2
```

The two warnings are retained in the full evidence bundle. The empty router DRC report is an ORFS observation only; it is not promoted to an independent or foundry-signoff DRC claim.

## Claim boundary

This result establishes that one registered, source-bound backend completed synthesis, floorplanning, placement, CTS, routing, fill, GDS merge, SPEF extraction, and final timing analysis under the declared contract.

It does **not** establish:

```text
matched three-backend physical comparison
post-physical functional or sequential equivalence
independent DRC
LVS
activity-based power
validated post-layout PEX
foundry sign-off
fabricated-silicon behavior
```

The permanent next layer must pin the immutable container digest, reproduce this contract for all three matched registered backends, execute two isolated attempts per backend, and bind every physical output back to the registered source digests.
