# IHP routed PVT corner timing research

This research layer asks a narrow question:

> How do the three matched registered implementations behave under the official
> IHP SG13G2 slow, typical, and fast Liberty views when the same routed netlist,
> extracted SPEF, and SDC are analyzed with OpenSTA?

It is intentionally downstream of the matched physical evidence and upstream of
any sign-off or statistical-variation claim.

## Evidence chain

```text
registered source binding
  -> matched OpenROAD placement and routing
  -> repeatable final Verilog / SPEF / SDC
  -> official IHP Open PDK commit
  -> slow / typical / fast Liberty selection
  -> two OpenSTA attempts per backend/corner
  -> parsed worst setup slack and TNS
  -> tight-clock negative control
```

The research matrix is:

```text
3 backends × 3 Liberty corners × 2 identical attempts
```

Backends:

```text
shared_dag
naive_shift_add
constant_multipliers
```

Corners are selected fail-closed from the official IHP Open PDK checkout. The
selector requires one unambiguous standard-cell Liberty for each of:

```text
slow:  nominally 1.08 V / 125 °C
 typ:  nominally 1.20 V / 25 °C
fast:  nominally 1.32 V / -40 °C
```

The exact files and SHA-256 digests are recorded in the generated evidence; the
labels above are not accepted as substitutes for artifact identity.

## Inputs

The probe consumes the qualified matched physical artifact rather than looking
for arbitrary `6_final.*` files. For each backend it resolves through the run
manifest and verifies:

```text
openroad_run.json SHA-256
final routed Verilog SHA-256
final SPEF SHA-256
final SDC SHA-256
top-module identity
physical-evidence prerequisites
```

A stale path from another runner is not trusted. Artifact resolution falls back
to a unique digest match and rejects ambiguity.

## Repeatability

OpenSTA is executed twice for every backend/corner. The two attempts must agree
on:

```text
worst setup slack
slack status
TNS, when emitted by the tool
```

Raw Tcl, stdout, and stderr are retained with their SHA-256 digests. Byte-level
stdout identity is not required because tool banners and environment paths are
not timing metrics.

## Negative control

For every backend, the typical-corner SDC is copied and only the first
`create_clock -period` value is replaced with a 0.05 ns period. The same routed
netlist, SPEF, Liberty, and OpenSTA binary must then report negative setup slack.

This verifies that the timing analysis responds to a materially stronger
constraint. It is not a substitute for checking every timing arc or variation
model.

## Claim boundary

A successful research run may state:

```json
{
  "physical_evidence_prerequisite_verified": true,
  "routed_netlists_bound_by_digest": true,
  "routed_spef_bound_by_digest": true,
  "official_ihp_open_pdk_commit_pinned": true,
  "three_liberty_corners_bound_by_digest": true,
  "all_backend_corner_analyses_completed": true,
  "two_attempt_repeatability_verified": true,
  "tight_clock_negative_control_violated": true,
  "multi_corner_timing_observed": true
}
```

It must keep these false:

```json
{
  "comparative_pvt_claim_enabled": false,
  "ocv_aocv_pocv_analyzed": false,
  "statistical_variation_analyzed": false,
  "crosstalk_delay_analyzed": false,
  "foundry_signoff_sta_performed": false,
  "foundry_signoff_complete": false,
  "silicon_verified": false
}
```

Three deterministic Liberty views do not cover within-die variation, on-chip
variation derates, AOCV/POCV tables, voltage droop, thermal gradients, coupling
noise, package effects, aging, or foundry sign-off methodology.

## Promotion requirements

This branch is not intended to merge as-is. Promotion into permanent evidence
requires:

1. a successful research artifact whose corner files and metrics are inspected;
2. elimination of any filename-only assumptions discovered during the run;
3. same-head consumption of permanent physical and post-physical prerequisites;
4. a versioned contract and compact regression reference;
5. a raw-report replay validator;
6. a negative control exercised through the permanent binder;
7. README and roadmap updates in the clean promotion PR;
8. removal of this research workflow and branch after promotion.
