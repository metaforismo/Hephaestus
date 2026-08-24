# Routed IHP SG13G2 PVT corner evidence

This layer characterizes the three matched registered routed tiles under three
official IHP SG13G2 standard-cell Liberty views.

It answers a deliberately narrow question:

> Under the same routed Verilog, extracted SPEF, SDC, OpenSTA build, and PDK
> commit, what setup timing does each implementation report at the selected
> slow, typical, and fast corners?

## Prerequisite chain

```text
verified integer contract
  -> matched combinational RTL
  -> registered streaming wrapper
  -> matched placement and routing
  -> physical repeatability
  -> post-physical clock-edge equivalence
  -> routed Verilog / SPEF / SDC digest binding
  -> IHP slow / typ / fast Liberty digest binding
  -> two OpenSTA attempts per backend/corner
  -> tight-clock negative control
  -> versioned regression reference
```

The permanent workflow consumes physical and post-physical artifacts from the
same `Pinned IHP OpenROAD physical evidence` workflow run and requires that run
to use the exact current head SHA.

## Matrix

```text
3 backends × 3 corners × 2 attempts = 18 positive analyses
3 backend-specific tight-clock controls
```

Backends:

```text
shared_dag
naive_shift_add
constant_multipliers
```

Corner order:

```text
slow
typ
fast
```

The nominal labels in the contract are explanatory. The executable authority is
the combination of:

```text
IHP Open PDK commit
relative Liberty path
Liberty SHA-256
OpenSTA binary SHA-256
routed Verilog SHA-256
routed SPEF SHA-256
SDC SHA-256
```

## Artifact resolution

The workflow does not trust an arbitrary `6_final.v` or `6_final.spef` found in
a directory. It starts from each `openroad_run.json`, checks the recorded digest,
and requires one unique matching artifact below the downloaded physical bundle.
Absolute paths, traversal, symlinks, missing files, duplicate digest matches, and
ambiguous attempt-one manifests fail closed.

## Repeatability

Every positive corner is analyzed twice. The two attempts must agree on:

```text
worst setup slack
slack status
TNS when emitted by OpenSTA
```

The comparison is numerical with an absolute tolerance of `1e-9`; raw Tcl,
stdout, and stderr remain preserved and digest-bound. Tool banners or runner
paths are not treated as timing metrics.

## Negative control

For each backend, the typical-corner SDC is copied and only the first
`create_clock -period` value is changed to `0.05 ns`. The same routed netlist,
SPEF, Liberty, and OpenSTA binary must report negative setup slack.

This verifies that the analysis is responsive to a materially stronger timing
constraint. It does not prove coverage of every timing arc, exception, or
variation model.

## Regression reference

The compact reference pins, for each backend:

```text
top module
routed Verilog digest
routed SPEF digest
SDC digest
three Liberty digests
three parsed metric records
negative-control period and outcome
```

A permanent run is qualified only when this stable projection matches the
versioned reference exactly. Runtime, logs, and absolute runner paths remain in
the full evidence bundle but are not regression identities.

## Claim boundary

A qualified result may claim:

```json
{
  "physical_evidence_prerequisite_verified": true,
  "post_physical_equivalence_prerequisite_verified": true,
  "routed_netlists_bound_by_digest": true,
  "routed_spef_bound_by_digest": true,
  "official_ihp_open_pdk_commit_pinned": true,
  "three_liberty_corners_bound_by_digest": true,
  "all_backend_corner_analyses_completed": true,
  "two_attempt_repeatability_verified": true,
  "tight_clock_negative_control_violated": true,
  "multi_corner_timing_observed": true,
  "comparative_pvt_claim_enabled": true
}
```

It must keep these false:

```json
{
  "ocv_aocv_pocv_analyzed": false,
  "statistical_variation_analyzed": false,
  "crosstalk_delay_analyzed": false,
  "foundry_signoff_sta_performed": false,
  "foundry_signoff_complete": false,
  "silicon_verified": false
}
```

`comparative_pvt_claim_enabled` means the three implementations may be compared
under this exact deterministic corner contract. It does not mean the ordering is
universal across floorplans, workloads, libraries, process lots, voltage droop,
thermal gradients, aging, package parasitics, statistical variation, or silicon.
