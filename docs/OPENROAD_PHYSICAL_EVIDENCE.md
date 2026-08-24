# Matched registered IHP OpenROAD physical evidence

Hephaestus runs the three registered 4×6 backends through one pinned IHP SG13G2 OpenROAD Flow
Scripts contract:

```text
shared_dag
naive_shift_add
constant_multipliers
```

Each backend is executed twice in isolation. The physical layer binds the exact registered source,
ORFS image, configuration, scripts, manifests, GDS, DEF, final Verilog, OpenDB, SPEF, metadata, and
selected metrics.

## Common boundary

```text
platform:          IHP SG13G2
clock:             rising-edge clk
period:            4.0 ns
input delay:       0.2 ns
output delay:      0.2 ns
clock uncertainty: 0.1 ns
input driver:      sg13g2_buf_4
output load:       0.01 pF
die:               0,0 → 240,240 µm
core:              20,20 → 220,220 µm
placement density: 0.50
routing:           Metal2 through Metal5
attempts/backend:  2
```

The ORFS image is pinned by immutable repository digest. Proprietary PDK collateral is not stored in
the repository.

## Repeatability semantics

The binder requires:

- byte-identical final Verilog;
- byte-identical DEF;
- identical parsed physical structure and selected metrics;
- GDS equality after normalizing only `BGNLIB` and `BGNSTR` timestamp payloads;
- SPEF equality after normalizing only the single `*DATE` field;
- matching source and physical-manifest identities.

Raw GDS, SPEF, OpenDB, reports, and hashes remain preserved. No geometry, connectivity, parasitic,
or routed-netlist difference is normalized away.

A real negative control mutates one source-binding digest in an actual run manifest. The binder must
reject it.

## Qualified physical observations

| Backend | Final instance area | Standard cells | DEF components | Nets | Routed wire | Vias | Setup slack | Observed fmax |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shared_dag` | **16,180.8** | **1,211** | **3,741** | **1,262** | **27,281** | **6,575** | 0.643739 ns | 297.951 MHz |
| `naive_shift_add` | 17,418.2 | 1,335 | 3,809 | 1,386 | 29,638 | 7,445 | 0.570264 ns | 291.568 MHz |
| `constant_multipliers` | 17,530.7 | 1,351 | 3,843 | 1,402 | 32,425 | 7,537 | **0.884282 ns** | **320.953 MHz** |

All metrics are observations under this exact tool, node, floorplan, constraints, and microcase.

Relative to naive shift/add, the shared DAG records approximately:

- 7.10% less final instance area;
- 9.29% fewer standard cells;
- 7.95% less routed wire;
- 11.69% fewer vias;
- 2.19% higher observed fmax.

Relative to constant multipliers, the shared DAG records approximately:

- 7.70% less area;
- 10.36% fewer standard cells;
- 15.86% less routed wire;
- 12.76% fewer vias;
- 7.17% lower observed fmax.

The honest result is mixed: the shared DAG minimizes area and wiring, while the constant-multiplier
source is fastest after routing.

## Downstream functional qualification

The physical artifact itself intentionally keeps:

```text
post_physical_equivalence_verified = false
comparative_ppa_claim_enabled = false
```

A separate downstream artifact consumes the physical bundle from the same workflow run, binds both
routed attempts per backend, and proves them against the exact registered sources. Only that
post-physical artifact may set the two fields true.

See [Routed registered post-physical equivalence](POST_PHYSICAL_EQUIVALENCE.md).

## Important non-claims

- Internal route DRC count zero is not independent DRC cleanliness.
- SPEF generation is not validated PEX.
- Tool-emitted power is not activity-qualified power.
- Routed timing is not foundry sign-off STA.
- This microcase does not establish model-level energy, throughput, or universal superiority.
- DRC, LVS, power, PEX, sign-off, and silicon claims remain false until separate evidence layers
  qualify them with appropriate controls.
