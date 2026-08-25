# Post-physical evidence status

This document is the authoritative summary of the current routed evidence boundary for the
registered 4×6 IHP SG13G2 regression microcase.

## Qualified

The permanent workflow now establishes one continuous same-source chain:

```text
verified registered sources
  → 3 matched backends × 2 isolated pinned ORFS attempts
  → bound GDS / DEF / final Verilog / OpenDB / SPEF
  → physical repeatability and source-mutation control
  → reset-synchronized bounded source-vs-routed proof
  → steady-state temporal induction
  → data / valid-latency / reset negative controls
  → stable regression binding
  → exact six-SPEF and run-manifest binding
  → complete canonical RC-graph parsing and declared-capacitance checks
  → two-attempt semantic repeatability and nine SPEF fault controls
```

Qualified claims for this exact contract:

```json
{
  "registered_source_binding_verified": true,
  "both_physical_attempts_per_backend_bound": true,
  "all_three_routed_registered_implementations_equivalent": true,
  "data_corruption_negative_control_detected": true,
  "valid_latency_negative_control_detected": true,
  "reset_state_negative_control_detected": true,
  "post_physical_equivalence_verified": true,
  "comparative_ppa_claim_enabled": true,
  "physical_spef_binding_verified": true,
  "post_physical_equivalence_prerequisite_verified": true,
  "all_six_spef_files_parsed": true,
  "spef_units_and_structure_validated": true,
  "spef_declared_capacitance_consistency_verified": true,
  "spef_semantic_repeatability_verified": true,
  "spef_negative_controls_detected": true
}
```

The physical artifact remains immutable and correctly reports downstream equivalence as false. The
separate post-physical artifact consumes and hashes that physical artifact, proves both routed
attempts for all three backends, and is the only layer allowed to enable the routed-equivalence and
comparative-PPA claims. The SPEF artifact then consumes the physical and post-physical artifacts
from one successful exact-head physical workflow run and is the only layer allowed to enable the
SPEF semantic claims.

## Proof scope

The routed functional result is:

```text
two-state
zero-delay functional cell models
clock-edge transaction semantics
explicit reset sequence
registered 48-bit input and 48-bit output buses
one-cycle declared value/valid boundary
exact 4×6 regression microcase
```

The source interface has synchronous active-high reset. Routed IHP flip-flops expose asynchronous
reset pins; both sides are normalized with `async2sync`. The proof therefore covers the declared
clock-edge behavior after the explicit reset sequence. It does not cover arbitrary asynchronous
reset transitions between edges, metastability, analog reset behavior, or arbitrary unreset
power-up recovery.

## Routed SPEF semantic scope

The downstream SPEF artifact consumes the matched physical and post-physical artifacts produced by
one successful exact-head physical workflow run. It rechecks the run-manifest and raw SPEF digest
bindings for all six attempts, parses every supported record, converts units, and compares complete
canonical RC graphs between the two attempts of each backend.

For every routed net it checks connections, ground and coupling capacitances, resistance edges,
name-map references, finite non-negative values, represented resistance endpoints, and consistency
between the declared `*D_NET` capacitance and the parsed `*CAP` sum. Three controls per backend must
detect declared-capacitance drift, a valid resistance-value mutation, and an invalid unit.

This qualifies the semantics and repeatability of the SPEF files already emitted by the pinned
physical flow. It is not a fresh extraction from GDS/OpenDB and is not an independent judgment that
the parasitic model is physically correct. See
[Routed SPEF semantic evidence](SPEF_SEMANTIC_EVIDENCE.md).

## Still unqualified

```json
{
  "four_state_semantics_verified": false,
  "timing_annotated_functional_semantics_verified": false,
  "drc_clean": false,
  "lvs_clean": false,
  "power_estimated_with_activity": false,
  "fresh_parasitic_extraction_performed": false,
  "independent_pex_crosscheck_verified": false,
  "post_layout_pex_verified": false,
  "foundry_signoff_complete": false,
  "silicon_verified": false
}
```

A zero internal routing-DRC count is not an independent DRC result. Tool-emitted power observations
are not activity-qualified power. Even fully parsed, repeatable SPEF output is not fresh or
independently validated PEX. No result in this layer is foundry sign-off.

## Comparative physical observation

| Backend | Final instance area | Standard cells | Routed wire | Vias | Observed fmax |
|---|---:|---:|---:|---:|---:|
| `shared_dag` | **16,180.8** | **1,211** | **27,281** | **6,575** | 297.951 MHz |
| `naive_shift_add` | 17,418.2 | 1,335 | 29,638 | 7,445 | 291.568 MHz |
| `constant_multipliers` | 17,530.7 | 1,351 | 32,425 | 7,537 | **320.953 MHz** |

The result is mixed. The shared DAG is smallest and has the least wire and fewest vias; the
constant-multiplier implementation is fastest. This supports continued physical co-design rather
than a universal superiority claim.

## Provenance policy

Every qualifying artifact records:

- the exact checked-out Git revision;
- workflow run and attempt metadata;
- physical, prepared, registered, model, reference, script, log, routed-netlist, and SPEF SHA-256
  values;
- both physical attempts per backend;
- raw positive and negative proof or mutation artifacts;
- the explicit claim boundary.

Pull-request runs explicitly check out the PR branch-head SHA. Push runs use the exact triggering
commit SHA. The SPEF workflow does not hard-code a historical run ID: it resolves the successful
physical workflow for that exact head, downloads both prerequisite artifacts from that same run,
and records the upstream workflow run ID as provenance.
