# Matched RTL baselines

Hephaestus compares architecture families only after placing them under the same arithmetic and
interface contract. The first matched bundle contains three combinational implementations of the
same quantized integer matrix:

1. **Shared DAG** — the normal Hephaestus topology with cross-output partial-sum sharing.
2. **Naive shift/add** — independent balanced trees for every output, with no cross-output sharing.
3. **Constant multipliers** — one explicit multiplication operator per nonzero coefficient,
   followed by independent output accumulation.

These are source-RTL baselines. Generic synthesis may legally transform constant multipliers into
shifts or other logic. A future physical comparison must therefore preserve the raw RTL, synthesis
script, mapped netlist, cell library, constraints, and reports instead of reasoning from source
operator counts alone.

## Generate a matched bundle

```bash
python -m hephaestus compile examples/tiny_weights.json \
  --out build/matched/source \
  --module hephaestus_matched_source \
  --verify-samples 256

python -m hephaestus.baselines build/matched/source \
  --out build/matched/backends \
  --module hephaestus_matched \
  --verify-vectors 256 \
  --seed 17 \
  --simulate
```

The second command requires `iverilog` and `vvp` only when `--simulate` is supplied.

## Contract

All three modules use:

- identical flattened signed input ports;
- identical flattened signed output ports;
- the same input and accumulator widths;
- the exact same quantized `codes.npy` matrix;
- combinational latency of zero cycles;
- the integer core before external power-of-two row scaling;
- an accumulator width proven sufficient by the compiler;
- zero runtime coefficient retrieval operations.

The self-checking testbench applies deterministic corner cases, one-lane signed extrema, and a
seeded collection of pseudo-random packed input vectors. It fails when any backend differs from the
other two.

## Artifact bundle

```text
build/matched/backends/
├── shared_dag.sv
├── naive_shift_add.sv
├── constant_multipliers.sv
├── matched_testbench.sv
├── matched_manifest.json
├── iverilog.stdout.txt
├── iverilog.stderr.txt
├── simulation.stdout.txt
└── simulation.stderr.txt
```

The manifest records source-level operator counts, module names, contract dimensions, verification
seed and vector count, simulator versions, SHA-256 hashes, and strict claim boundaries.

## What this proves

A passing bundle proves that the three RTL descriptions agree for every vector executed by the
self-checking testbench under the declared integer contract. It does not yet prove:

- exhaustive formal equivalence;
- mapped area or maximum frequency;
- post-route wire length, congestion, buffering, or power;
- PEX correctness;
- superiority over a ROM/codebook architecture;
- token throughput or fabricated-silicon behavior.

Those require separate evidence levels and matched physical-design constraints.
