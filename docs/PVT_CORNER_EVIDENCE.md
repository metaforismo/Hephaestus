# Routed IHP SG13G2 PVT corner evidence

This layer characterizes the exact six routed registered implementations from the
qualified matched physical flow under three official IHP SG13G2 standard-cell
Liberty views.

It answers a narrow question:

> Under the same routed Verilog, SPEF, SDC, and OpenSTA method, how do the three
> matched implementations behave at the pinned slow, typical, and fast Liberty
> corners, and are those observations reproducible across both physical attempts
> and two independent analysis replays?

It is not foundry-signoff STA and it does not model statistical variation,
crosstalk, voltage drop, electromigration, thermal gradients, or silicon.

## Permanent matrix

```text
3 backends
× 2 independently generated physical attempts
× 3 Liberty corners
× 2 isolated OpenSTA replays
= 36 positive analyses
```

Backends:

```text
shared_dag
naive_shift_add
constant_multipliers
```

Corners:

| Label | Nominal condition | Exact Liberty |
|---|---|---|
| `slow` | 1.08 V / 125 °C | `sg13g2_stdcell_slow_1p08V_125C.lib` |
| `typ` | 1.20 V / 25 °C | `sg13g2_stdcell_typ_1p20V_25C.lib` |
| `fast` | 1.32 V / −40 °C | `sg13g2_stdcell_fast_1p32V_m40C.lib` |

The PDK commit, each Liberty Git blob, and each Liberty SHA-256 digest are pinned
in `configs/evidence/ihp_sg13g2_pvt_corner_v2.json`.

## Evidence chain

```text
qualified matched physical evidence
  + qualified post-physical sequential equivalence
  ↓
all six exact physical run manifests
  ↓
exact routed Verilog / SPEF / SDC binding
  ↓
pinned IHP Open PDK commit and three Liberty SHA-256 digests
  ↓
pinned OpenSTA source commit and captured binary/tool manifest
  ↓
36 positive analyses
  + 6 tight-clock negative controls
  ↓
raw-report replay
  ↓
stable regression projection
```

The source validator checks both the bound and original physical run manifests,
the post-physical manifest/netlist bindings, all source digests and file sizes,
the PDK commit, Liberty Git objects and SHA-256 values, and the OpenSTA tool
manifest. All manifest-derived paths are relative, root-confined, and symlink
free.

## Timing-completeness contract

Every OpenSTA execution emits and reparses an explicit report schema. A positive
analysis is rejected unless all of the following hold:

- the pinned OpenSTA `check_setup` command returns success;
- at least one clock exists after loading the exact SDC;
- at least one maximum-delay timing path exists;
- `report_parasitic_annotation` reports zero unannotated drivers;
- it also reports zero partially unannotated drivers;
- exactly one worst-setup-slack summary and one maximum TNS summary are present;
- slack sign, status, and total-negative-slack behavior are mutually consistent;
- the completion and exact corner markers are present;
- OpenSTA returns zero and emits no fatal diagnostic.

The script uses the non-deprecated `report_checks` path-count options from the
pinned OpenSTA source. Ordinary tool warnings are preserved in the raw logs and
are not blanket-normalized away; setup completeness is decided by the explicit
`check_setup` result and the machine-readable invariants above.

These checks establish complete annotation and timing coverage under this exact
OpenSTA/SPEF/SDC contract. They do not establish crosstalk-aware delay,
variation-aware sign-off, or foundry sign-off.

## Repeatability contract

For each routed physical attempt and corner, two OpenSTA executions must produce
identical worst setup slack, status, total negative slack, setup-check result,
clock/path counts, and SPEF annotation counts. Floating timing values must agree
to `1e-9 ns`; the discrete coverage fields must match exactly.

The two independently generated physical attempts for each backend must then
produce the same timing metrics at every corner. This second comparison is
separate from tool replay repeatability.

Every raw stdout/stderr/script/return-code file is SHA-256 bound and reparsed.
Recorded metrics that cannot be reconstructed from the raw report are rejected.

## Negative controls

Each of the six routed physical attempts receives a typical-corner tight-clock
control with a `0.05 ns` period.

A control qualifies only when:

- OpenSTA completes successfully;
- the same setup, clock/path, and SPEF-annotation checks pass;
- the raw report replays successfully;
- worst setup slack is negative;
- total negative slack is negative;
- the control slack is worse than the ordinary typical-corner result.

A crash, timeout, malformed report, missing marker, unrelated diagnostic, or
nonzero process return code does not count as successful fault detection.

## Reference policy

The versioned reference pins stable technical observations:

- PDK and OpenSTA source commits;
- Liberty SHA-256 values;
- routed Verilog, SDC, and date-normalized SPEF digests;
- per-backend, per-physical-attempt, per-corner timing and coverage metrics;
- replay counts and tight-clock control behavior;
- the exact claim boundary.

A reference can be created only from an exact bootstrap artifact in which
`comparative_pvt_claim_enabled` is still false. The bootstrap claim dictionary
must match the supported key set exactly; missing, injected, or prematurely
promoted claims are rejected. Strict validation records the final claim boundary
only after the stable projection matches.

The reference deliberately excludes GitHub run IDs, artifact IDs, runner paths,
raw log hashes, execution timestamps, and the OpenSTA binary hash because the
current build manifest explicitly does not claim bit-reproducible compiler
output. The binary hash remains preserved in every full evidence artifact.

## Claim boundary

After exact-head CI and reference validation, this layer may enable:

```text
multi_corner_timing_observed
comparative_pvt_claim_enabled
```

only for the exact registered 4×6 regression microcase and the declared
three-corner contract.

It must keep these false:

```text
ocv_analyzed
aocv_analyzed
pocv_analyzed
statistical_variation_analyzed
crosstalk_delay_analyzed
ir_drop_analyzed
electromigration_analyzed
thermal_analyzed
foundry_signoff_sta_performed
foundry_signoff_complete
silicon_verified
```

A Liberty-corner sweep is deterministic PVT characterization. It is not a
statistical variation model or sign-off timing closure.

## Reproduction

The permanent workflow obtains the physical and post-physical artifacts from the
successful physical workflow run associated with the exact current head SHA,
builds the repository-pinned OpenSTA binary, checks out the exact PDK commit, and
runs:

```bash
python -m hephaestus.pvt_corner run \
  build/pvt/physical \
  build/pvt/post-physical \
  --pdk build/pvt/ihp-open-pdk \
  --opensta build/pvt/opensta/opensta.bin \
  --opensta-manifest build/pvt/opensta/tool.json \
  --contract configs/evidence/ihp_sg13g2_pvt_corner_v2.json \
  --reference benchmarks/reference/ihp_sg13g2_pvt_corner_tiny_v2.json \
  --out build/pvt/evidence \
  --source-revision "$GITHUB_SHA" \
  --upstream-run-id "$UPSTREAM_RUN_ID"
```

Until the bootstrap artifact has been independently inspected and the versioned
reference has been committed, the workflow omits `--reference` and
`comparative_pvt_claim_enabled` remains false.
