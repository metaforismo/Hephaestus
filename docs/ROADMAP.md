# Roadmap

## M0 — Semantic spine — implemented

- Safe local matrix loading.
- Signed-power-of-two quantization with exact power-of-two row scales.
- Optional sensitivity weighting.
- Serializable constant-matrix IR.
- Global partial-sum sharing and hash-consed adder DAG.
- Synthesizable SystemVerilog emission.
- Randomized bit-exact verification and CI synthesis smoke test.
- Machine-readable claim manifest.

## M1 — Model frontend

- Read Hugging Face `config.json` and Safetensors index files.
- Select and stream sharded tensors without materializing a whole model.
- Normalize transposed and fused projection layouts.
- Build a graph IR for one supported decoder architecture.
- Preserve tokenizer/model licenses and provenance.

**Exit criterion:** compile every linear tensor of a small supported model and reproduce the
original graph numerically with simulated quantized operators.

## M2 — Hardware-aware quantization

- Activation calibration and second-moment collection.
- GPTQ/AWQ-style sensitivity objectives adapted to a discrete shift/add codebook.
- Mixed precision, structured sparsity, outlier/residual planes, and group scales.
- Accuracy evaluation on language-model and long-context tasks.
- Pareto search over quality, area, depth, fanout, and activity.

**Exit criterion:** an accuracy/PPA Pareto frontier that beats the v0 nearest-code baseline.

## M3 — Scalable constant-matrix synthesis

- Canonical signed digit and multi-constant multiplication rewrites.
- Scalable cross-output CSE and graph partitioning.
- Logic factoring, reassociation, retiming, and fanout buffering.
- Placement-aware clustering and congestion feedback.
- Deterministic compilation and cacheable per-tile artifacts.

**Exit criterion:** compile transformer-sized tiles without superlinear memory growth and show
post-synthesis savings over a direct multiplier/ROM baseline.

## M4 — Open-PDK RTL-to-GDS

- IHP SG13G2 or another production-accessible open-PDK backend.
- Clocked tile, SRAM/stream interfaces, reset, scan/DFT plan, and pad-ring wrapper.
- Yosys/OpenROAD/KLayout automation.
- DRC, LVS, STA, extraction, and switching-based power reports.
- Formal and gate-level equivalence.

**Exit criterion:** reproducible DRC/LVS-clean GDS and post-layout evidence from a clean checkout.

## M5 — First silicon

- Small MPW test chip, not a full LLM.
- On-chip vectors and signature checking.
- Frequency/voltage sweep, shmoo plots, thermal and power measurement.
- Compare RTL, post-layout simulation, and lab measurements.

**Exit criterion:** measured silicon executes a compiled matrix exactly and publishes honest
energy/operation and throughput data.

## M6 — Transformer subsystem

- Pipeline a full decoder block or a useful fixed-model subsystem.
- Integrate nonlinear and stateful operators.
- Define KV-cache and host/chiplet interfaces.
- Evaluate model update economics and residual programmability.

## M7 — Advanced-node qualification

- Foundry/Europractice approval and NDA.
- Port the proven tile to a FinFET PDK.
- Rebuild cost models with real libraries, RC, variation, IR drop, EM, yield, and reticle limits.
- Seek a shuttle, university program, industrial sponsor, or acquisition/partnership only after
  the open-node evidence is strong.
