# Hephaestus

**An open model-to-metal research compiler for neural-network weights that become circuit
topology.**

Hephaestus takes a constant two-dimensional tensor, quantizes it to signed powers of two, finds
reusable partial sums, and emits synthesizable SystemVerilog. The generated direct-logic core has
no weight array, weight address bus, codebook ROM, or runtime coefficient-fetch path. Coefficients
exist as shifts, signs, wires, and shared addition nodes.

> **Implemented and tested:** JSON/NumPy/Safetensors tensor access, Hugging Face sharded-index
> resolution, bounded tensor slicing, signed-power-of-two quantization, a serializable shared-adder
> DAG, RTL emission, structural evidence, matched RTL backends, reproducible generic Yosys
> evidence, bounded exhaustive Yosys-SAT equivalence, pinned IHP SG13G2 standard-cell mapping,
> exhaustive SAT equivalence of mapped standard-cell netlists, technology-aware ABC area-delay
> sweeps under versioned input-driver and output-load assumptions, and exhaustive SAT proof of
> every distinct netlist produced by that sweep.
>
> **Not claimed:** a complete transformer compiler, timing or power closure, competitive
> post-layout PPA, 40,000 tokens/s, a 7 nm tapeout, extracted energy, or measured silicon.

## Why this direction

Conventional accelerators repeatedly move model parameters through a memory hierarchy. A fixed
model creates a different design point: compile stable parameters into the implementation and move
activations instead. Hephaestus explores a direct-logic branch of that space:

```text
checkpoint tensor or bounded tile
              │
              ▼
power-of-two, sensitivity-aware quantization
              │
              ▼
constant-matrix ZeroFetch IR
              │
              ▼
global common-subexpression elimination
              │
              ▼
shift / sign / shared-adder DAG
              │
              ▼
SystemVerilog → Yosys → OpenROAD → GDS → DRC/LVS/PEX
```

The central invariant is deliberately narrow and testable:

```text
runtime weight reads per compiled matrix-vector operation = 0
```

Activations, accumulators, control state, KV cache, residuals, and model I/O still move. Zero weight
fetch is not zero memory, zero physical representation, or an energy result.

## Quick start

```bash
python -m pip install -e ".[dev]"

hephaestus compile examples/tiny_weights.json \
  --out build/tiny \
  --module hephaestus_tiny \
  --verify-samples 256

./scripts/check_rtl.sh build/tiny/hephaestus_tiny.sv hephaestus_tiny
```

The output directory contains:

```text
build/tiny/
├── hephaestus_tiny.sv       # fixed-topology RTL
├── manifest.json            # source, quantization, topology, verification, claim stage
├── plan.json                # serializable addition DAG
├── codes.npy                # quantized integer coefficient oracle
└── row_scale_exponents.npy  # exact 2^e output-scale metadata
```

## Hugging Face Safetensors

Hephaestus never deserializes pickle checkpoints. It can inspect a direct Safetensors file, a
Hugging Face `*.safetensors.index.json`, or a directory containing one unambiguous checkpoint:

```bash
hephaestus tensors /models/llama-checkpoint
```

A bounded tile can be compiled without materializing the other model shards or the rest of the
selected tensor:

```bash
hephaestus compile /models/llama-checkpoint \
  --tensor-key model.layers.0.mlp.up_proj.weight \
  --rows 0:128 \
  --columns 0:256 \
  --out build/layer0-up-tile
```

The manifest records the original shape, selected ranges, actual shard, index digest when present,
and a canonical digest of exactly the floating-point values consumed by the compiler. It does not
hash an entire multi-gigabyte shard merely to compile a small tile.

## Current numerical representation

The first quantizer uses a codebook such as:

```text
{-4, -2, -1, 0, 1, 2, 4}
```

Each output row receives an exactly representable power-of-two scale. The integer core computes the
code matrix; the row exponents remain explicit metadata for later fixed-point scheduling. An
optional one-dimensional activation-importance vector or full matrix can weight quantization error.

The lowerer turns every nonzero code into one signed shift of an input, greedily shares repeated
partial sums across outputs, and hash-conses the remaining balanced addition trees. A Python
reference evaluator checks the serialized graph with arbitrary-precision integers, avoiding false
success caused by NumPy `int64` overflow.

## First pinned mapped result

For the bundled 4×6 integer microcase, the same IHP SG13G2 typical Liberty and Yosys/ABC flow maps
the shared DAG to 492 cells and 5350.6656 Liberty area units. The matched naive shift/add backend
maps to 574 cells and 6285.0816 units; the explicit constant-multiplier source maps to 574 cells and
6248.7936 units.

A downstream proof loads functional Boolean models from the same pinned Liberty and symbolically
proves all three mapped netlists against an independent reference derived directly from `codes.npy`.
The proof covers every defined 48-bit input assignment and requires a synthetic output fault to
produce a counterexample.

This result is a reproducible standard-cell mapped-area estimate plus bounded mapped-netlist
functional equivalence. It is not placed area, timing, power, or post-layout PPA. See
[Pinned IHP mapped synthesis](docs/MAPPED_SYNTHESIS.md) and
[Mapped standard-cell equivalence](docs/MAPPED_FORMAL_EQUIVALENCE.md) for the exact flows, digests,
negative controls, and claim boundaries.

## First technology-aware area-delay result

The same 4×6 matched backends were also mapped with the pinned IHP Liberty while declaring
`sg13g2_buf_4` as the primary-input driving cell and 10 fF of load on each primary output. The value
below is the achieved delay printed by ABC `stime -p`; it is not the requested `-D` target.

| Backend | Cells | Liberty area | ABC delay |
|---|---:|---:|---:|
| Shared Hephaestus DAG | **497** | **5439.5712** | **2029.49 ps** |
| Naive output-local shift/add | 577 | 6339.5136 | 2183.17 ps |
| Explicit constant-multiplier source | 578 | 6334.0704 | 2270.49 ps |

At this pre-layout point, the shared DAG reduces area-delay product by 20.2358% relative to naive
shift/add and by 23.2375% relative to the explicit constant-multiplier source. The evidence also
sweeps 1000, 2000, 4000, 8000, and 16000 ps targets, records whether each target is actually met,
collapses duplicate coordinates, and retains the two observed Pareto points per backend.

A separate downstream proof now verifies the Boolean semantics of every netlist produced by the
sweep. The 18 labeled runs collapse to six distinct mapped-Verilog SHA-256 digests; all six are
proved exhaustively over the defined 48-bit input space. A label can reuse a proof only when its
mapped Verilog is byte-identical to the representative, and an injected output fault must still
produce a counterexample.

This is a technology-aware ABC estimate plus a separate functional-equivalence layer. It is not
sign-off STA, SDC timing closure, placed or routed timing, parasitic extraction, or measured
silicon. See [ABC area-delay evidence](docs/ABC_AREA_DELAY.md) and
[ABC sweep mapped equivalence](docs/ABC_AREA_DELAY_FORMAL.md) for the assumptions, exact reports,
proof grouping, negative control, regression references, and claim boundaries.

## Research thesis

Hephaestus should not be a clone of a mask-ROM accelerator. The strongest route is numerical,
logical, and physical co-design:

1. **Quantize for physical cost, not only model error.** Optimize quality together with adder
   count, depth, fanout, congestion, wire length, and switching.
2. **Synthesize the constant matrix directly.** Explore signed-digit recoding, shared sums,
   structured sparsity, low-rank or transform factorization, and placement-aware tile partitioning.
3. **Retain a small programmable residual plane.** Keep outliers, adapters, calibration values,
   and model patches configurable while the dominant base is fixed.
4. **Close the physical feedback loop.** Feed synthesis and place-and-route evidence back into
   quantization and graph rewriting. A smaller mathematical graph can lose after buffering.
5. **Use accessible process evidence first.** Establish equivalence, PPA, DRC/LVS, and PEX on an
   open or accessible node before making advanced-node product claims.

See [Strategy](docs/STRATEGY.md), [Architecture](docs/ARCHITECTURE.md),
[Roadmap](docs/ROADMAP.md), [Benchmarking](docs/BENCHMARKING.md),
[Structural evidence](docs/EVIDENCE.md), [Matched RTL baselines](docs/MATCHED_BASELINES.md),
[Generic Yosys evidence](docs/SYNTHESIS_EVIDENCE.md),
[Formal equivalence](docs/FORMAL_EQUIVALENCE.md),
[Pinned IHP mapped synthesis](docs/MAPPED_SYNTHESIS.md),
[Mapped standard-cell equivalence](docs/MAPPED_FORMAL_EQUIVALENCE.md),
[ABC area-delay evidence](docs/ABC_AREA_DELAY.md),
[ABC sweep mapped equivalence](docs/ABC_AREA_DELAY_FORMAL.md),
[Patent landscape](docs/IP_LANDSCAPE.md), [Foundry path](docs/FOUNDRY_PATH.md), and
[Research plan](docs/RESEARCH.md).

## Project status

Hephaestus is pre-alpha research software. Generated RTL has not been fabricated. Patent notes are
technical reading, not a freedom-to-operate opinion. Commercial work requires qualified digital,
physical-design, verification, DFT, package, safety, security, and patent specialists.

## License

Apache-2.0. Proprietary PDKs, standard-cell libraries, foundry collateral, and third-party model
checkpoints are not part of this repository.
