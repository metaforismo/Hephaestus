# Matched registered OpenROAD physical evidence

This layer moves the three verified registered integer cores through the same IHP SG13G2
RTL-to-GDS flow. It is intentionally downstream of the registered-tile contract and upstream of
post-physical equivalence, independent DRC/LVS, activity-based power, and validated PEX.

```text
verified integer contract
  → three matched combinational backends
  → exhaustive source-core equivalence
  → identical registered streaming boundaries
  → qualifying shared-DAG ORFS smoke run
  → one pinned ORFS container digest
  → three backends × two clean physical attempts
  → repeatability binding
  → preserved GDS / DEF / Verilog / OpenDB / SPEF / metadata
```

## Why the smoke run is a prerequisite

The first physical step was deliberately limited to the registered shared DAG. That run established
that the committed 4 ns clock, 240 µm square die, 200 µm square core, 0.50 placement density, and
Metal2–Metal5 routing boundary can complete on the IHP SG13G2 ORFS platform.

The smoke reference pins:

- the exact registered shared-DAG core and wrapper digests;
- the official ORFS image `RepoDigest`, not the mutable `latest` tag;
- the committed floorplan and timing boundary;
- the final GDS, DEF, mapped Verilog, OpenDB, and SPEF observations;
- a claim boundary that remains explicitly single-backend and non-comparative.

The permanent matched flow refuses to start when the current registered shared-DAG source differs
from that qualifying smoke source or when the physical contract drifts.

## Common contract

The versioned contract is:

```text
configs/physical/ihp_sg13g2_openroad_registered_v1.json
```

All three backends use:

```text
technology:          IHP SG13G2
clock:               rising-edge `clk`
period:              4.0 ns
input delay:         0.2 ns
output delay:        0.2 ns
clock uncertainty:   0.1 ns
input driver:        sg13g2_buf_4
output load:         0.01 pF per output
reset:               synchronous active-high
value latency:       one cycle
valid latency:       one cycle
initiation interval: one cycle
die:                 0,0 → 240,240 µm
core:                20,20 → 220,220 µm
placement density:   0.50
routing layers:      Metal2 → Metal5
OpenROAD threads:    one
attempts:            two per backend
```

The fixed die and core dimensions matter. Allowing ORFS to resize each design from its synthesized
area would give the three architectures different wire and congestion opportunities and would not
be a matched physical comparison.

## Preparation gate

`python -m hephaestus.openroad_physical prepare` validates:

- `hephaestus.registered-matched-tiles.v1` and its successful schedule-based claims;
- the pinned registered regression contract and all six source RTL digests;
- the qualifying OpenROAD smoke reference;
- the exact shared-DAG source identity observed by the smoke run;
- one immutable ORFS image digest;
- safe, in-root, non-symlink artifact paths;
- zero runtime coefficient reads for every backend.

It then stages the exact registered bundle and emits one ORFS design directory per backend. Only the
top module and its paired core RTL differ. Timing, floorplan, density, routing, and tool identity are
common.

## Physical attempts

Each matrix entry is an isolated ORFS execution:

```text
shared_dag             attempt 1
shared_dag             attempt 2
naive_shift_add        attempt 1
naive_shift_add        attempt 2
constant_multipliers   attempt 1
constant_multipliers   attempt 2
```

The runner pulls the pinned image by digest, verifies that Docker exposes that exact `RepoDigest`,
captures the embedded tool versions, and preserves the complete flow directories and logs.

A successful run must emit non-empty:

```text
6_final.gds
6_final.def
6_final.v
6_final.odb
6_final.spef
metadata.json
```

Every file is recorded with its byte size and SHA-256 digest. The final DEF is also parsed for die
geometry, component count, net count, special-net count, rows, and routing tracks. Numeric ORFS
quality-of-result fields are retained separately from runtime, host, timestamp, and version noise.

## Repeatability rule

Raw GDSII embeds library and structure timestamps, so blindly requiring identical file hashes can
turn creation dates into a false physical regression. Hephaestus therefore records both the raw GDS
hash and a second digest that zeros only `BGNLIB` and `BGNSTR` date payloads while preserving every
other GDSII record byte.

Two attempts qualify only when all of these agree:

- final DEF SHA-256;
- final mapped-Verilog SHA-256;
- final SPEF SHA-256;
- timestamp-normalized GDS SHA-256;
- parsed DEF metrics;
- selected stable numeric ORFS metrics.

Raw GDS and OpenDB byte equality remain visible observations, but they are not silently promoted
into requirements when only embedded timestamps or implementation serialization differ.

## Negative control

The permanent workflow first binds all six real manifests. It then mutates the registered-wrapper
digest in one copied run manifest and requires the same binder to reject it with a source-binding
error. The test is performed against a real qualifying run manifest, not a separate toy parser.

## Reproduction

Preparation is independent of Docker:

```bash
python -m hephaestus.openroad_physical prepare \
  build/openroad-physical/registered \
  --registered-reference benchmarks/reference/registered_matched_tiles_tiny_v1.json \
  --probe-reference benchmarks/reference/ihp_sg13g2_openroad_registered_shared_dag_smoke_v1.json \
  --contract configs/physical/ihp_sg13g2_openroad_registered_v1.json \
  --out build/openroad-physical/prepared
```

One physical attempt is:

```bash
python -m hephaestus.openroad_physical run \
  build/openroad-physical/prepared/prepared.json \
  --backend shared_dag \
  --attempt 1 \
  --out build/openroad-physical/runs/shared_dag/attempt-1
```

After all six attempts:

```bash
python -m hephaestus.openroad_physical bind \
  build/openroad-physical/prepared/prepared.json \
  --runs build/openroad-physical/runs \
  --out build/openroad-physical/evidence
```

The permanent CI entry point is `.github/workflows/openroad-physical-evidence.yml`.

## Claim boundary

A passing first physical bundle may claim:

- exact registered-source and contract binding;
- use of one pinned ORFS image digest;
- placement and routing of all three matched backends;
- final GDS and SPEF generation;
- two qualifying attempts per backend;
- the declared physical repeatability checks;
- preservation of observed physical metrics under one common boundary.

It deliberately does **not** enable a comparative PPA claim yet. The final routed netlists have not
received downstream sequential equivalence evidence, so observed area, timing, congestion, or power
fields are evidence to preserve rather than a basis for declaring an architectural winner.

The following remain false:

```text
post-physical equivalence verified
comparative PPA claim enabled
independent DRC clean
LVS clean
activity-based power estimated
validated post-layout PEX
foundry sign-off complete
silicon verified
```

The next evidence layer should prove each routed sequential netlist against its exact registered
source under reset, then bind those proofs back to the physical artifact digests before comparative
physical results are published.
