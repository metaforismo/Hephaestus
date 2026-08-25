# Routed SPEF semantic evidence

This layer validates the six routed SPEF files emitted by the matched IHP SG13G2
physical flow. It is downstream of both the matched physical evidence and the
qualified routed sequential-equivalence evidence.

It answers a narrow question:

> Are the exact SPEF files already emitted by the qualified physical runs
> digest-bound, structurally parseable, semantically repeatable, and internally
> consistent as RC-network descriptions?

It does **not** perform a new parasitic extraction and it does not independently
establish that the extracted values are physically correct.

## Evidence chain

```text
registered source evidence
  -> 3 matched backends x 2 pinned ORFS attempts
  -> bound final routed SPEF and run manifests
  -> qualified source-to-routed sequential equivalence
  -> strict IEEE 1481-1999 SPEF parser
  -> canonical RC graph per physical attempt
  -> attempt-1 / attempt-2 semantic comparison
  -> backend-specific negative controls
  -> stable regression projection
```

The permanent builder rejects a missing or incomplete prerequisite, a different
post-physical source revision, a post-physical attempt that binds another physical
run manifest, an unsafe artifact path, a symlink, a digest or size mismatch, and a
reference projection that differs from the pinned regression contract.

## Parser contract

The parser consumes the complete document rather than searching for a few marker
lines. The supported contract requires exactly one of each header directive used
by the current OpenROAD producer, followed by `*NAME_MAP`, `*PORTS`, and one or
more complete `*D_NET` sections.

For every net it validates:

- connections and their direction;
- ground and coupling capacitances;
- resistance edges;
- unique positive CAP and RES record indices;
- finite, non-negative RC values;
- defined name-map references;
- non-self-connected coupling and resistance edges;
- resistance endpoints that are represented in the net's connection or
  capacitance graph;
- agreement between the declared `*D_NET` capacitance and the parsed `*CAP` sum.

The declared-capacitance comparison uses a relative tolerance of `1e-5` and an
absolute tolerance of `1e-12 pF`. This accommodates the decimal rounding present
in the real OpenROAD files without accepting material drift.

## Semantic canonicalization

The canonical digest is independent of:

- `*NAME_MAP` numeric identifiers;
- record order inside the supported sections;
- equivalent SPEF unit spellings after conversion to ns, pF, ohm, and henry;
- producer date, vendor, program, and version metadata.

It deliberately retains:

- SPEF standard;
- design name;
- semantic `*DESIGN_FLOW` values;
- divider, delimiter, and bus-delimiter contract;
- resolved port and net names;
- connection direction and instance cell type;
- every ground capacitance, coupling capacitance, and resistance value.

The full evidence also preserves each raw SPEF SHA-256 digest, file size,
`*DATE`-normalized digest, producer metadata, run-manifest digest, canonical
summary, and copied source artifact.

## Regression microcase

The reference was seeded from the exact qualified PR #40 artifacts at source
revision:

```text
b8210960b1d250f104f26d4e17497b90ade4966b
```

A new pull-request or `main` run must reproduce the stable semantic projection
from newly generated same-workflow artifacts. The source revision and raw file
hashes are evidence provenance, not stable regression fields.

| Backend | Nets | Nodes | Ground caps | Coupling caps | Resistors | Declared capacitance | Total resistance |
|---|---:|---:|---:|---:|---:|---:|---:|
| `shared_dag` | 1,262 | 5,959 | 5,959 | 13,502 | 4,697 | 3.703339197 pF | 144,063.8678 ohm |
| `naive_shift_add` | 1,386 | 6,638 | 6,638 | 15,590 | 5,252 | 4.0755655916 pF | 162,585.9118 ohm |
| `constant_multipliers` | 1,402 | 6,743 | 6,743 | 16,378 | 5,341 | 4.5813538824 pF | 165,656.1936 ohm |

These values characterize one registered 4x6 routed microcase. They are not
model-level energy, sign-off extraction, or universal architecture results.

## Negative controls

Each backend uses its first qualified physical attempt as the source for three
independent controls:

1. `declared_capacitance` changes one `*D_NET` total while leaving its `*CAP`
   records intact. The parser must reject the resulting inconsistency for the
   expected reason.
2. `resistance` changes one resistance value while preserving valid SPEF syntax.
   The parser must accept the document and produce a different canonical RC-graph
   digest.
3. `unit` replaces the capacitance unit with an unsupported unit. The parser must
   reject it for the expected unit-contract reason.

A qualifying artifact therefore contains:

```text
6 positive SPEF parses
9 backend-specific negative controls
```

A crash, missing file, unrelated parse error, or unchanged semantic digest does
not count as successful fault detection.

## Claim boundary

A qualified artifact may set only these new claims to true:

```text
physical_spef_binding_verified
post_physical_equivalence_prerequisite_verified
all_six_spef_files_parsed
spef_units_and_structure_validated
spef_declared_capacitance_consistency_verified
spef_semantic_repeatability_verified
spef_negative_controls_detected
```

It must keep these false:

```text
fresh_parasitic_extraction_performed
independent_pex_crosscheck_verified
post_layout_pex_verified
foundry_signoff_complete
silicon_verified
```

The distinction is intentional. Parsing, binding, and comparing a producer's
existing SPEF output does not prove that the producer extracted the layout
correctly.

## Reproduction

Given the physical artifact, post-physical artifact, and pinned reference:

```bash
python -m hephaestus.spef_semantic \
  build/spef-semantic/physical \
  build/spef-semantic/post-physical \
  --reference \
    benchmarks/reference/ihp_sg13g2_spef_semantic_tiny_v1.json \
  --out build/spef-semantic/evidence \
  --source-revision "$GITHUB_SHA"
```

The output contains `spef_semantic_evidence.json`, a concise `SUMMARY.md`, copied
source manifests and reference, six copied routed SPEF files, six semantic
summaries, and the nine mutation artifacts.

## Next evidence boundary

The next PEX-related step must start from the exact routed OpenDB or layout and
perform a fresh extraction using pinned OpenRCX rules and a pinned tool image.
Two isolated extractions per physical attempt can establish extraction
reproducibility and compare fresh results with the bundled SPEF. Even that would
not, by itself, establish an independent PEX cross-check or foundry sign-off.
