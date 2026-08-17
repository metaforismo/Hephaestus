# Hephaestus

**A model-to-metal research compiler for neural networks whose weights become circuit topology.**

Hephaestus currently takes one constant 2-D weight matrix, quantizes it to signed powers of two,
finds reusable partial sums, and emits synthesizable SystemVerilog. The generated core contains
no weight array, weight address bus, codebook ROM, or runtime weight-fetch path. Its coefficients
exist as shifts, signs, wires, and shared addition nodes.

> **What is real today:** a tested matrix → quantization → shared adder DAG → RTL path with
> bit-exact integer verification.
>
> **What is not claimed yet:** a complete Hugging Face transformer compiler, competitive PPA,
> 40,000 tokens/s, a 7 nm tapeout, post-layout energy, or measured silicon.

## Why this direction

Conventional accelerators repeatedly move model parameters through a memory hierarchy. A fixed
model creates a different design point: compile stable parameters into the implementation and
move activations instead. Hephaestus explores a direct-logic branch of that space:

```text
checkpoint tensor
      │
      ▼
power-of-two, sensitivity-aware quantization
      │
      ▼
constant-matrix IR
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

The central invariant is narrow and testable:

```text
runtime weight reads per compiled matrix-vector operation = 0
```

Activations, accumulators, control state, KV cache, residuals, and model I/O still move. “Zero
weight fetch” is not “zero memory” and it is not itself an energy result.

## Quick start

```bash
python -m pip install -e ".[dev]"

hephaestus compile examples/tiny_weights.json \
  --out build/tiny \
  --module hephaestus_tiny \
  --verify-samples 256

./scripts/check_rtl.sh build/tiny/hephaestus_tiny.sv hephaestus_tiny
```

The build directory contains:

```text
build/tiny/
├── hephaestus_tiny.sv       # fixed-topology RTL
├── manifest.json            # quantization, cost and claim evidence
├── plan.json                # serializable addition DAG
├── codes.npy                # quantized integer coefficient oracle
└── row_scale_exponents.npy  # exact 2^e output scales
```

Local Safetensors support is optional:

```bash
python -m pip install -e ".[hf]"
hephaestus compile model.safetensors --tensor-key model.layers.0.mlp.up_proj.weight
```

Hephaestus does not deserialize pickle checkpoints.

## Current compiler

The first quantizer uses a codebook such as:

```text
{-4, -2, -1, 0, 1, 2, 4}
```

Each row receives an exactly representable power-of-two scale. The compiler then lowers every
nonzero coefficient to one signed shift of an input and performs global common-subexpression
elimination before building balanced adder trees. A Python evaluator proves that the serialized
DAG equals the integer matrix for randomly generated input vectors.

This is deliberately small enough to inspect. It establishes the semantic and evidence spine
before adding model import, calibration, physical design, and advanced-node backends.

## Research thesis

Hephaestus should not be a clone of a mask-ROM accelerator. The strongest route is co-design:

1. **Quantization for physical cost, not only perplexity.** Optimize accuracy together with
   adder count, depth, fanout, routing demand, and switching activity.
2. **Direct constant-matrix synthesis.** Use shift/add/subtract networks, structured sparsity,
   factorization, and cross-output sharing instead of reading one encoded weight at a time.
3. **A small programmable residual plane.** Keep outliers, adapters, calibration values, or model
   updates configurable while the dominant base matrix is fixed.
4. **Physical feedback.** Feed synthesis and placement results back into quantization and graph
   rewriting. A mathematically smaller netlist can still lose after routing.
5. **Open-PDK evidence first.** Prove bit-exactness, PPA, DRC/LVS and PEX on an accessible node
   before treating an NDA-gated 7 nm flow as the development environment.

See [Strategy](docs/STRATEGY.md), [Architecture](docs/ARCHITECTURE.md),
[Roadmap](docs/ROADMAP.md), [Benchmarking](docs/BENCHMARKING.md),
[Taalas IP landscape](docs/IP_LANDSCAPE.md), and [Foundry path](docs/FOUNDRY_PATH.md).

## Project status

Hephaestus is pre-alpha research software. Generated RTL has not yet been fabricated. Patent
notes are technical reading, not a freedom-to-operate opinion. Any commercial implementation
needs qualified semiconductor engineering, verification, security review, and patent counsel.

## License

Apache-2.0. Proprietary PDKs and third-party model checkpoints are not part of this repository.
