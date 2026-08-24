# Routed registered post-physical equivalence

This layer proves the narrow downstream statement that the exact registered source implementation
for each matched backend is sequentially equivalent to the exact routed `6_final.v` emitted by the
same-head physical workflow.

It is deliberately compositional:

```text
independent arithmetic reference
  → exhaustive source-core proof
  → registered source contract
  → exact registered source RTL
  → two routed physical attempts per backend
  → reset-synchronized bounded base case
  → steady-state temporal induction
```

The routed proof does not reintroduce the full arithmetic miter. That upstream contract is already
bound by the registered and physical manifests. The permanent builder validates that chain before
comparing each source wrapper to each routed netlist.

## Same-run architecture

The permanent entry point is:

```bash
python -m hephaestus.post_physical_equivalence \
  build/post-physical/physical \
  --models flows/openroad/post_physical_equivalence/ihp_sg13g2_formal_models.v \
  --reference benchmarks/reference/ihp_sg13g2_post_physical_equivalence_tiny_v1.json \
  --out build/post-physical/equivalence \
  --source-revision "$GITHUB_SHA"
```

It runs as a downstream job inside `.github/workflows/openroad-physical-evidence.yml`. The job uses
`actions/download-artifact` without a run identifier, so it can only consume the physical artifact
created by the preceding `prepare → physical-run → bind` jobs in that workflow execution. No
historical workflow run is part of the permanent contract.

## Binding contract

Before invoking Yosys, the builder independently verifies:

- the physical, prepared, and registered manifest schemas and claim boundaries;
- the SHA-256 chain from physical evidence to the prepared and registered manifests;
- the exact three-backend set and registered clock/reset/latency contract;
- both bound and original `openroad_run.json` copies for all six attempts;
- backend, attempt, source-core, source-wrapper, prepared, and registered identities in every run;
- the final routed-Verilog path and digest in both the run manifest and physical evidence;
- byte-identical routed Verilog across the two attempts required by physical repeatability;
- the functional IHP cell-model digest and the pinned regression reference.

Every proof directory preserves the exact source core, source wrapper, routed netlist, functional
cell models, generated wrapper, Yosys scripts, stdout, stderr, and their digests.

## Why the proof has two obligations

Yosys documents `equiv_induct` as a weak temporal-induction relation: it establishes that two
circuits cannot diverge after their observable equivalence points have already remained equal for
the selected sequence length. It is therefore not used alone.

For every routed attempt, the permanent gate requires both:

1. a bounded reset-synchronized base case that establishes four consecutive equal observable
   cycles after reset; and
2. a steady-state induction proof with `equiv_induct -seq 4`.

The base case is generated directly from the `equiv_make` result with `equiv_miter -assert`, then
checked over five symbolic cycles:

```text
cycle 1: reset asserted
cycles 2–5: reset released
assertions checked on cycles 2–5
initial state: arbitrary but defined
inputs: arbitrary but defined
```

The steady-state obligation independently runs:

```text
equiv_make
equiv_struct -maxiter 20
equiv_simple
equiv_induct -seq 4
equiv_status -assert
```

A positive result is accepted only when the bounded SAT proof reaches the SAT pass with a nonzero
set of equivalence points and finds no counterexample, while the final induction status reports a
nonzero number of cells, all cells proven, zero unproven cells, a zero exit status, no timeout, and
the final success marker.

## Reset semantics

The registered source contract uses synchronous active-high reset. The routed IHP implementation
contains technology flip-flops with asynchronous reset pins. Both sides are normalized with Yosys
`async2sync` before the two proof obligations.

The qualified statement is therefore limited to the declared **clock-edge transaction behavior
after the explicit reset sequence**. It does not claim equivalence for arbitrary asynchronous reset
transitions between clock edges, analog reset behavior, metastability, or arbitrary unreset power-up
recovery.

## Negative controls

One independent control of each class is required for every backend:

- **data:** invert one routed output bit;
- **valid latency:** insert an additional valid register;
- **reset state:** disconnect the routed implementation from the declared reset.

Each control must fail both obligations in the expected way:

- the bounded reset miter must reach SAT and produce a real counterexample;
- the induction flow must reach `equiv_status -assert` and leave one or more equivalence cells
  unproven.

Syntax errors, missing inputs, timeouts, tool crashes, empty equivalence sets, and generic nonzero
exit codes are not accepted as negative-control success.

## Regression policy

The reference pins only stable invariants:

- exact source-core, source-wrapper, routed-netlist, functional-model, generated-wrapper, and proof
  script digests;
- the pinned Yosys version;
- bounded-reset sequence, proof method, and induction sequence length;
- proved/unproven induction cell counts;
- expected negative-control counterexample and unproven-cell observations;
- the explicit claim boundary.

It does not pin raw runtime, CPU time, memory use, log hashes, GitHub run IDs, or other execution
noise. The full evidence manifest still records the exact current manifests, logs, source revision,
and workflow provenance.

## Claim boundary

Only a qualifying exact-head run enables these statements for the registered 4×6 IHP SG13G2
regression microcase:

```text
post_physical_equivalence_verified = true
comparative_ppa_claim_enabled = true
```

The comparison remains limited to the declared two-state, zero-delay, clock-edge functional
semantics and the common physical boundary. It does not establish four-state behavior,
timing-annotated behavior, arbitrary asynchronous-reset-event equivalence, independent DRC, LVS,
validated PEX, activity-based power, foundry sign-off, universal architectural superiority, or
silicon behavior.
