# Hephaestus

**An open model-to-metal research compiler for neural-network weights that become circuit topology.**

Hephaestus takes a fixed two-dimensional tensor, quantizes it, factors repeated arithmetic, emits
synthesizable SystemVerilog, and carries the resulting registered designs through formal proof and
an open RTL-to-GDS flow. The current direct-logic core has no runtime coefficient array, address
bus, codebook ROM, or weight-fetch path: coefficients exist as shifts, signs, wires, and additions.

The central invariant is deliberately narrow:

```text
runtime weight reads per compiled matrix-vector operation = 0
```

That does **not** mean zero memory, zero activation traffic, zero KV-cache traffic, zero state, zero
wire cost, or automatically lower energy. It means only that the compiled matrix coefficients are
not fetched at runtime.

## Current evidence boundary

Implemented and continuously checked:

- safe JSON, NumPy, NPZ, Safetensors, and sharded Hugging Face tensor input;
- bounded tensor slicing without unsafe pickle checkpoint loading;
- signed-power-of-two quantization with exact power-of-two row scales;
- serializable shared-adder DAG lowering and synthesizable flattened-port RTL;
- matched `shared_dag`, `naive_shift_add`, and `constant_multipliers` backends;
- generic Yosys synthesis, exhaustive bounded combinational SAT, and negative controls;
- pinned IHP SG13G2 mapping, mapped-netlist formal equivalence, ABC area-delay, and OpenSTA;
- identical registered streaming wrappers with latency one and initiation interval one;
- three backends × two repeatable pinned OpenROAD Flow Scripts attempts;
- exact routed GDS, DEF, Verilog, OpenDB, SPEF, manifests, and SHA-256 provenance;
- same-run post-physical sequential equivalence for both routed attempts of every backend;
- independent data, valid-latency, and reset negative controls for both proof obligations;
- strict semantic parsing and canonical RC-network validation of all six routed SPEF files;
- declared-capacitance consistency, two-attempt semantic repeatability, and nine SPEF fault controls.

Not claimed:

- a complete transformer compiler;
- universal superiority of hardwired weights;
- four-state or timing-annotated routed equivalence;
- arbitrary asynchronous-reset-event equivalence;
- independent DRC or LVS cleanliness;
- activity-qualified power, fresh parasitic re-extraction, or independently validated PEX;
- foundry sign-off, 7 nm readiness, token/s claims, or measured silicon.

See [Post-physical status](docs/POST_PHYSICAL_STATUS.md) for the authoritative claim matrix.

## Why this direction

A fixed model is not the same hardware-design problem as a fully programmable accelerator.
Conventional accelerators repeatedly move parameters through a memory hierarchy. Hephaestus
explores whether stable coefficients can instead become implementation topology:

```text
checkpoint tensor or bounded tile
              ↓
hardware-aware quantization
              ↓
constant-matrix IR
              ↓
signed shifts and signs
              ↓
shared partial-sum DAG
              ↓
registered SystemVerilog
              ↓
formal proof
              ↓
Yosys / OpenSTA / OpenROAD
              ↓
GDS / DEF / OpenDB / SPEF
              ↓
DRC / LVS / PEX / power / silicon, when independently qualified
```

The long-term question is not whether the project can draw a GDS file. It can. The question is
whether joint numerical, logical, and physical compilation can produce an end-to-end advantage
once routing, buffering, switching, model quality, variation, and update economics are included.

## Qualified 4×6 routed microcase

The current physical regression uses one common IHP SG13G2 contract for all three registered
backends and runs each implementation twice. Both attempts per backend must agree under narrowly
specified normalization rules, and both exact routed netlists are then proved against the exact
registered source implementation.

| Backend | Final instance area | Standard cells | Routed wire length | Vias | Observed fmax |
|---|---:|---:|---:|---:|---:|
| `shared_dag` | **16,180.8** | **1,211** | **27,281** | **6,575** | 297.951 MHz |
| `naive_shift_add` | 17,418.2 | 1,335 | 29,638 | 7,445 | 291.568 MHz |
| `constant_multipliers` | 17,530.7 | 1,351 | 32,425 | 7,537 | **320.953 MHz** |

For this exact microcase and physical contract:

- the shared DAG is the smallest and has the least routed wire and fewest vias;
- it is slightly faster than naive shift/add;
- the constant-multiplier source is faster than the shared DAG after routing;
- therefore the physical result is **mixed**, not “the shared DAG wins everything.”

Relative to naive shift/add, the shared DAG has approximately 7.10% less final instance area,
9.29% fewer standard cells, 7.95% less routed wire, 11.69% fewer vias, and 2.19% higher observed
fmax. Relative to constant multipliers, it has approximately 7.70% less area, 10.36% fewer cells,
15.86% less wire, and 12.76% fewer vias, but approximately 7.17% lower observed fmax.

These are routed observations for one 4×6 registered microcase. They are not model-level PPA,
activity-qualified power, sign-off timing, or a universal architecture result. Internal routing DRC
counts and tool-emitted power numbers remain observations until independent downstream evidence is
qualified.

See [Matched OpenROAD physical evidence](docs/OPENROAD_PHYSICAL_EVIDENCE.md) and
[Post-physical equivalence](docs/POST_PHYSICAL_EQUIVALENCE.md).

## Post-physical equivalence

The permanent proof is compositional:

```text
independent arithmetic oracle
  → exhaustively proved source core
  → registered source implementation
  → two routed attempts per backend
  → five-cycle reset-synchronized bounded SAT base case
  → steady-state temporal induction
```

For each of six routed attempts, the bounded proof starts from arbitrary defined state, asserts
reset in cycle one, releases it for cycles two through five, and proves the observable equivalence
points over the four cycles required by the induction premise. A separate flow then runs
`equiv_struct`, `equiv_simple`, `equiv_induct -seq 4`, and `equiv_status -assert`.

Every backend also has three fault classes:

- data corruption;
- an extra valid-latency register;
- routed reset disconnection.

Each fault must produce a bounded SAT counterexample and leave at least one unproven equivalence
point in the induction flow. The exact number of unproven points is retained in raw evidence but is
not treated as a stable solver invariant.

A qualifying artifact may set:

```text
post_physical_equivalence_verified = true
comparative_ppa_claim_enabled = true
```

only for the exact two-state, zero-delay, clock-edge 4×6 contract.

## Routed SPEF semantic validation

A downstream exact-head workflow consumes the physical and post-physical artifacts from the same
successful physical workflow run. It then parses all six routed SPEF files into complete canonical
RC graphs rather than treating file existence or a raw digest as PEX validation.

The permanent gate validates headers, name-map references, ports, connections, ground and coupling
capacitances, resistance edges, unit conversion, represented resistance endpoints, and agreement
between every declared `*D_NET` capacitance and its parsed `*CAP` sum. Equivalent name-map IDs,
record ordering, producer timestamps, and equivalent units do not change the canonical digest;
design flow, names, connectivity, cell types, and RC values do.

For the exact 4×6 routed regression:

| Backend | Nets | Nodes | Ground caps | Coupling caps | Resistors | Total declared capacitance | Total resistance |
|---|---:|---:|---:|---:|---:|---:|---:|
| `shared_dag` | **1,262** | **5,959** | **5,959** | **13,502** | **4,697** | **3.703339197 pF** | **144,063.8678 Ω** |
| `naive_shift_add` | 1,386 | 6,638 | 6,638 | 15,590 | 5,252 | 4.0755655916 pF | 162,585.9118 Ω |
| `constant_multipliers` | 1,402 | 6,743 | 6,743 | 16,378 | 5,341 | 4.5813538824 pF | 165,656.1936 Ω |

Each backend also requires declared-capacitance corruption, resistance-network corruption, and unit
corruption controls. The result qualifies binding, structural/semantic parsing, internal
capacitance consistency, and repeatability of the producer's existing SPEF output. It does **not**
perform a fresh extraction or independently prove that the parasitics are physically correct.

See [Routed SPEF semantic evidence](docs/SPEF_SEMANTIC_EVIDENCE.md).

## Earlier evidence ladder

The routed result sits above several separately scoped layers.

### Generic Yosys structure

| Backend | Generic post-techmap cells |
|---|---:|
| Shared DAG | **823** |
| Naive shift/add | 943 |
| Constant multipliers | 955 |

These are generic structural counts, not standard-cell PPA.

### Pinned IHP mapped area

| Backend | Cells | Liberty area units |
|---|---:|---:|
| Shared DAG | **492** | **5,350.6656** |
| Naive shift/add | 574 | 6,285.0816 |
| Constant multipliers | 574 | 6,248.7936 |

The exact mapped netlists are separately proved against an independent arithmetic reference.
Liberty area is not placed silicon area.

### Pinned ABC area-delay observation

| Backend | Cells | Liberty area | ABC delay |
|---|---:|---:|---:|
| Shared DAG | **497** | **5,439.5712** | **2,029.49 ps** |
| Naive shift/add | 577 | 6,339.5136 | 2,183.17 ps |
| Constant multipliers | 578 | 6,334.0704 | 2,270.49 ps |

ABC delay is not sign-off STA.

### Formally bound pre-layout OpenSTA observation

| Backend | OpenSTA data delay | Worst slack under 4 ns reporting period |
|---|---:|---:|
| Shared DAG | **2.187032223 ns** | 1.812967777 ns |
| Naive shift/add | 2.356311440 ns | 1.643688560 ns |
| Constant multipliers | 2.390366912 ns | 1.609633088 ns |

This layer has no routed parasitics and is intentionally distinct from the physical result above.

## Quick start

```bash
python -m pip install -e ".[dev]"

hephaestus compile examples/tiny_weights.json \
  --out build/tiny \
  --module hephaestus_tiny \
  --verify-samples 256

./scripts/check_rtl.sh build/tiny/hephaestus_tiny.sv hephaestus_tiny
```

The compiler emits:

```text
build/tiny/
├── hephaestus_tiny.sv
├── manifest.json
├── plan.json
├── codes.npy
└── row_scale_exponents.npy
```

## Safe model input

Hephaestus never deserializes pickle checkpoints. It can inspect direct Safetensors, a Hugging Face
`*.safetensors.index.json`, or an unambiguous checkpoint directory:

```bash
hephaestus tensors /models/checkpoint

hephaestus compile /models/checkpoint \
  --tensor-key model.layers.0.mlp.up_proj.weight \
  --rows 0:128 \
  --columns 0:256 \
  --out build/layer0-up-tile
```

The manifest records the selected tensor shape and ranges, actual shard, descriptor provenance, and
a canonical digest of exactly the floating-point values consumed by the compiler.

## Current numerical representation

The first quantizer uses a signed power-of-two/zero codebook such as:

```text
{-4, -2, -1, 0, 1, 2, 4}
```

Each row has an exactly representable power-of-two scale. An optional activation-importance vector
or full matrix weights quantization error. The integer core computes the code matrix; row scale
exponents remain explicit metadata for later fixed-point scheduling.

## Research direction

The next architectural gains should come from numerical, logical, and physical co-design:

1. canonical signed digit and multiple-constant-multiplication rewrites;
2. scalable CSE, algebraic factoring, retiming, and fanout-aware transformations;
3. placement-, congestion-, wire-, and switching-aware graph cost functions;
4. activation-calibrated quantization, mixed precision, sparsity, and outlier planes;
5. a mostly fixed base with a small programmable residual plane;
6. a closed loop from quantization to P&R measurements and back;
7. larger tiles and model-quality evaluation only after the microcase evidence remains trustworthy;
8. independent DRC, LVS, PEX, activity-based power, and eventually a small MPW test chip.

Hephaestus is an independent research architecture. Public patents and papers are technical input,
not a freedom-to-operate opinion. Proprietary PDKs, foundry collateral, and third-party checkpoints
do not belong in this repository.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- [Matched baselines](docs/MATCHED_BASELINES.md)
- [Mapped synthesis](docs/MAPPED_SYNTHESIS.md)
- [Mapped formal equivalence](docs/MAPPED_FORMAL_EQUIVALENCE.md)
- [ABC area-delay](docs/ABC_AREA_DELAY.md)
- [OpenSTA timing](docs/OPENSTA_TIMING_EVIDENCE.md)
- [Registered tiles](docs/REGISTERED_TILES.md)
- [OpenROAD physical evidence](docs/OPENROAD_PHYSICAL_EVIDENCE.md)
- [Post-physical equivalence](docs/POST_PHYSICAL_EQUIVALENCE.md)
- [Routed SPEF semantic evidence](docs/SPEF_SEMANTIC_EVIDENCE.md)
- [Post-physical status](docs/POST_PHYSICAL_STATUS.md)
- [Patent landscape](docs/IP_LANDSCAPE.md)
- [Foundry path](docs/FOUNDRY_PATH.md)

## Project status

Hephaestus is pre-alpha research software. No generated design has been fabricated. Commercial work
requires qualified verification, physical-design, DFT, package, reliability, security, and patent
specialists.

## License

Apache-2.0.
