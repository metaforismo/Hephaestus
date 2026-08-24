# Routed registered post-physical equivalence

This permanent layer proves the narrow downstream statement that each exact registered source
implementation is sequentially equivalent to each exact routed `6_final.v` emitted by the same
source revision.

## Compositional evidence chain

```text
independent arithmetic reference
  → exhaustive source-core equivalence
  → registered source implementation
  → two routed physical attempts per backend
  → reset-synchronized bounded base case
  → steady-state temporal induction
```

The routed proof does not rebuild the expensive full arithmetic miter. Upstream manifests already
bind the independently generated reference to each source core and registered wrapper. The
post-physical builder verifies that entire digest chain before comparing source and routed RTL.

## Same-source workflow

The permanent entry point is:

```bash
python -m hephaestus.post_physical_equivalence \
  build/post-physical/physical \
  --models flows/openroad/post_physical_equivalence/ihp_sg13g2_formal_models.v \
  --reference benchmarks/reference/ihp_sg13g2_post_physical_equivalence_tiny_v1.json \
  --out build/post-physical/equivalence \
  --source-revision "$SOURCE_REVISION"
```

It runs inside `.github/workflows/openroad-physical-evidence.yml` after the physical binder and
downloads the artifact produced by that same workflow execution. Pull-request jobs explicitly
check out the PR branch-head SHA; push/manual jobs use the triggering SHA. There is no historical
run ID or expiring cross-run prerequisite in permanent code.

## Binding contract

Before proof, the builder rejects any mismatch in:

- physical, prepared, registered, and run-manifest schemas;
- prerequisite claim boundaries;
- exact backend and attempt identities;
- registered dimensions, latency, valid, and reset contract;
- physical-to-prepared and physical-to-registered SHA-256 links;
- bound versus original `openroad_run.json` bytes;
- source core and wrapper identities for every attempt;
- routed-Verilog metadata and content;
- both-attempt repeatability;
- functional IHP model and regression-reference digests;
- unsafe, overlapping, pre-existing, or destructive output paths.

Each artifact preserves source RTL, routed RTL, functional models, generated wrappers, proof
scripts, stdout, stderr, manifests, tool identity, revision metadata, and SHA-256 provenance.

## Why two proof obligations

Yosys describes `equiv_induct` as a weak temporal induction relation: it proves non-divergence once
observable points have already remained equal for the selected sequence length. It is therefore not
used by itself.

Every routed attempt must pass both obligations.

### 1. Reset-synchronized bounded base case

The builder converts the `equiv_make` result into assertions with `equiv_miter -assert` and invokes
bounded SAT over five symbolic cycles:

```text
cycle 1: reset asserted
cycles 2–5: reset released
assertions checked across cycles 2–5
initial state: arbitrary but defined
inputs: arbitrary but defined
```

The proof is accepted only when the SAT pass actually runs, the equivalence set is nonempty, no
counterexample exists, the success marker is present, the process exits cleanly, and no timeout
occurs.

### 2. Steady-state induction

```text
equiv_make
equiv_struct -maxiter 20
equiv_simple
equiv_induct -seq 4
equiv_status -assert
```

The result is accepted only when a nonzero equivalence set is reported, every point is proven, zero
points remain unproven, the final success marker is present, and the process exits cleanly.

For the pinned 48-bit input/48-bit output microcase, both obligations cover 49 observable
points: 48 data bits plus valid.

## Reset semantics

The registered source uses synchronous active-high reset. Routed IHP flip-flops expose asynchronous
reset pins. Both sides are normalized with `async2sync` before proof.

The qualified statement is limited to clock-edge transaction behavior after the explicit reset
sequence. It excludes arbitrary asynchronous transitions between edges, analog reset behavior,
metastability, and arbitrary unreset power-up recovery.

## Negative controls

Every backend has three independent fault classes:

- **data:** invert one routed output bit;
- **valid:** insert an extra valid register;
- **reset:** disconnect routed reset.

Each control must:

1. reach bounded SAT with a nonempty equivalence set and produce a real counterexample; and
2. reach `equiv_status -assert` and leave at least one equivalence point unproven.

Syntax errors, absent inputs, empty equivalence sets, timeouts, crashes, and generic nonzero exits do
not count as successful controls.

The full artifact records the exact unproven-cell count. The regression reference intentionally
pins the semantic predicate `unproven_equivalence_detected = true`, not the solver decomposition
count: the same effective fault may leave one or many points unresolved depending on structural
proof progress. A zero count is always rejected.

## Regression policy

Pinned stable invariants include:

- exact source-core, wrapper, routed-netlist, model, generated-wrapper, and script digests;
- pinned Yosys version;
- reset sequence and proof method;
- positive equivalence-cell coverage;
- bounded negative-control counterexamples;
- induction negative-control detection;
- explicit true and false claim fields.

Not pinned:

- CPU time, memory, log hashes, GitHub run ID, runner image noise;
- the exact number of negative-control points left unproven.

Those observations remain in the complete evidence manifest and raw logs.

## Qualified claim boundary

A qualifying artifact may enable, for the exact registered 4×6 IHP SG13G2 contract:

```text
post_physical_equivalence_verified = true
comparative_ppa_claim_enabled = true
```

It does not establish:

- four-state or timing-annotated behavior;
- arbitrary asynchronous-reset-event equivalence;
- independent DRC or LVS;
- activity-qualified power;
- validated PEX;
- foundry sign-off;
- model-level or universal architectural superiority;
- silicon behavior.
