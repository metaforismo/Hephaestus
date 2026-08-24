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
  → compositional sequential equivalence
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
cell models, generated wrapper, Yosys script, stdout, stderr, and their digests.

## Proof method

For each of three backends and each of two physical attempts, Yosys normalizes the exact registered
source and routed implementation independently, then runs:

```text
equiv_make
equiv_struct -maxiter 20
equiv_simple
equiv_induct -seq 4
equiv_status -assert
```

A positive result is accepted only when the final `equiv_status` reports a nonzero number of cells,
all cells proven, zero unproven cells, a zero exit status, no timeout, and the final success marker.
Intermediate `equiv_simple` failures are not treated as proof failure when `equiv_induct` later
proves the complete workset.

## Negative controls

One independent control of each class is required for every backend:

- **data:** invert one routed output bit;
- **valid latency:** insert an additional valid register;
- **reset state:** disconnect the routed implementation from the declared reset.

A negative control passes only when Yosys reaches `equiv_status -assert`, exits nonzero, reports one
or more unproven equivalence cells, does not emit the success marker, and does not time out. Syntax,
tool, or missing-input errors are therefore not accepted as counterexamples.

## Regression policy

The reference pins only stable invariants:

- exact source-core, source-wrapper, routed-netlist, functional-model, generated-wrapper, and proof
  script digests;
- the pinned Yosys version;
- proof method and sequence length;
- proved/unproven cell counts;
- expected negative-control unproven counts;
- the explicit claim boundary.

It does not pin raw runtime, CPU time, memory use, log hashes, GitHub run IDs, or other execution
noise. The full evidence manifest still records the exact current manifests, logs, source revision,
and workflow provenance.

## Claim boundary

A qualifying exact-head run enables these statements for the registered 4×6 IHP SG13G2 regression
microcase:

```text
post_physical_equivalence_verified = true
comparative_ppa_claim_enabled = true
```

The comparison remains limited to the declared two-state, zero-delay functional semantics and the
common physical boundary. It does not establish four-state behavior, timing-annotated behavior,
independent DRC, LVS, validated PEX, activity-based power, foundry sign-off, universal architectural
superiority, or silicon behavior.
