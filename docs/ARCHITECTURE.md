# Architecture

## 1. Definition of zero-weight-fetch

A compiled Hephaestus core is zero-weight-fetch when its runtime interface and synthesized logic
contain no operation whose purpose is to retrieve a coefficient from a weight storage structure.
The coefficient must instead be expressed by topology: constant shifts, sign selection, gates,
wire connectivity, and arithmetic nodes.

This definition does not exclude:

- activation registers or streams;
- accumulators and pipeline registers;
- KV-cache storage;
- control and status registers;
- a separately identified residual, adapter, or outlier plane;
- physical capacitance on wires that encode connectivity.

The manifest records the narrower claim so it cannot silently become “no memory traffic.”

## 2. Compiler layers

### Frontend

The frontend accepts JSON, NumPy arrays with pickle disabled, NPZ, and optional Safetensors.
Initial compilation is tensor-local: one 2-D matrix at a time. A model graph importer will later
recover operator ordering, tensor shapes, nonlinearities, normalization, attention, and residual
edges from a Hugging Face checkpoint/configuration.

### Quantization

The v0 quantizer maps floating weights to signed powers of two and chooses a per-row scale that is
itself an exact power of two. An optional importance vector weights quantization error, allowing
activation second moments or Hessian proxies to influence code selection.

Future quantizers should expose a multi-objective loss:

```text
L = task_error
  + λ_area · estimated_area
  + λ_delay · critical_path
  + λ_wire · routing_proxy
  + λ_toggle · switching_proxy
  + λ_mask · customization_cost
```

### ZeroFetch IR

The serializable plan contains only:

- activation atoms: `(input, sign, shift)`;
- binary addition nodes;
- output references;
- exact integer widths and structural metrics.

There is no weight-load instruction in the IR.

### Graph optimizer

The current optimizer greedily shares repeated adjacent pairs across output expressions and then
hash-conses balanced adder trees. This is a correct baseline, not the final algorithm. Large
matrices need scalable column-pair search, signed-digit recoding, graph partitioning, retiming,
fanout control, and physical-aware rewrite selection.

### RTL backend

The current backend emits a combinational, flattened-port SystemVerilog module. Flattened ports
keep the result compatible with conservative open-source synthesis frontends. A later backend will
insert pipeline stages under explicit latency/throughput constraints and emit formal equivalence
properties.

### Physical backend

The intended open flow is:

```text
SystemVerilog
  → Yosys synthesis
  → OpenROAD floorplan / placement / CTS / routing
  → KLayout GDS assembly and verification
  → extraction / timing / power / waveform evidence
```

PDK-specific content must stay outside this repository.

## 3. Planned architecture families

### A. Direct-logic matrix

Each weight becomes a signed shift or a small constant multiplier; outputs share partial sums.
This is the implemented family and the cleanest place to establish evidence.

### B. Factorized topology

Approximate or exact matrix factorization can replace one dense matrix with a product of sparse,
structured, low-rank, butterfly, circulant, or transform matrices. It trades additional stages
for much lower wire and adder cost.

### C. Fixed base plus programmable residual

A dominant base matrix is hardwired while a small high-precision residual/outlier/LoRA plane is
stored conventionally. The optimization target is total system energy and model fidelity, not an
ideological ban on every programmable bit.

### D. Metal-programmed distribution network

A shared value generator distributes products through model-specific high-level metal. This may
be efficient, but it overlaps a crowded patent area and can become routing-dominated. It remains a
research backend, not the default architecture.

## 4. System-level constraints

A transformer is not only GEMM. A complete design must account for embedding/output projection,
RMSNorm or LayerNorm, rotary position encoding, attention score and softmax, KV-cache bandwidth,
residual paths, sampling, host I/O, batching, clock/power delivery, thermal limits, yield, and the
cost of changing a model.

Therefore the first silicon target should be a measured mat-vec tile or a small transformer block,
not an 8B-model reticle-scale chip.
