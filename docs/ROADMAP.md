# Roadmap

## M0 — Semantic spine — implemented

- Safe JSON, NumPy, NPZ, Safetensors, and sharded-index matrix loading.
- Bounded tensor slicing without unsafe pickle deserialization.
- Signed-power-of-two quantization with exact row scales and optional sensitivity weighting.
- Serializable constant-matrix IR, cross-output sharing, and hash-consed adder DAG.
- Synthesizable SystemVerilog emission and arbitrary-precision reference evaluation.
- Machine-readable manifests, SHA-256 provenance, tests, and CI.

## M1 — Model frontend

- Read model configuration and graph structure for one supported decoder architecture.
- Normalize transposed, fused, and sharded projection layouts.
- Compile every linear tensor of a small model without materializing unrelated shards.
- Preserve tokenizer, checkpoint, and license provenance.

**Exit criterion:** reproduce one supported quantized decoder graph numerically with simulated
operators.

## M2 — Hardware-aware quantization

- Activation calibration and second-moment collection.
- GPTQ/AWQ-style sensitivity objectives adapted to shift/add and physical cost.
- Mixed precision, structured sparsity, group scales, outliers, and residual planes.
- Quality evaluation on language-model and long-context tasks.
- Pareto search over quality, area, depth, fanout, switching, wire, and congestion.

**Exit criterion:** a quality/physical-cost frontier that improves on the v0 nearest-code baseline.

## M3 — Scalable constant-matrix synthesis

- Canonical signed digit and multiple-constant-multiplication rewrites.
- Scalable cross-output CSE, algebraic factoring, and graph partitioning.
- Retiming, fanout buffering, placement-aware clustering, and congestion feedback.
- Deterministic compilation and cacheable per-tile artifacts.

**Exit criterion:** compile transformer-sized tiles without superlinear memory growth and show
repeatable post-route trade-offs against matched baselines.

## M3.5 — Registered matched tile — implemented for the 4×6 regression

- Three arithmetic backends under one integer contract.
- Exhaustively proved source cores.
- Identical registered input/output/valid/reset wrappers.
- One-cycle value and valid latency, initiation interval one.
- Continuous traffic, bubbles, reset checks, and simulation negative control.

## M4 — Open-PDK RTL-to-GDS — physical and functional chain qualified for the microcase

Implemented:

- one immutable ORFS image and one common IHP SG13G2 physical contract;
- three registered backends × two isolated physical attempts;
- synthesis, floorplanning, placement, CTS, routing, GDS, OpenDB, DEF, and SPEF;
- strict source and artifact digest binding;
- narrowly normalized physical repeatability and a real manifest-mutation control;
- both routed attempts per backend proved against the exact registered source;
- five-cycle reset-synchronized bounded SAT base cases;
- separate steady-state temporal induction;
- data, valid-latency, and reset controls for both obligations;
- stable regression reference and same-run evidence artifact;
- comparative physical observations enabled for the exact contract;
- all six routed SPEF files bound to their physical manifests and parsed into canonical RC graphs;
- declared-capacitance consistency, semantic two-attempt repeatability, and nine SPEF fault controls.

Still required before M4 is complete:

- independent DRC with a versioned open IHP rule deck and invalid-geometry control;
- LVS against the exact expected netlist with layout- and schematic-side controls;
- fresh OpenRCX extraction from the exact routed OpenDB under pinned rules and isolated replays;
- a sufficiently independent PEX cross-check before enabling `post_layout_pex_verified`;
- deterministic activity generation and routed activity-based power;
- scan/DFT planning, physical I/O, and eventually a pad-ring wrapper;
- variation and reliability layers only where real collateral supports them.

**Current boundary:** routed functional comparison and semantic validation of the six emitted SPEF
files are qualified for one 4×6 microcase. Fresh extraction, independent PEX validation, DRC, LVS,
activity power, foundry sign-off, and silicon remain false.

**Exit criterion:** reproducible independently checked DRC/LVS-clean GDS plus validated
post-layout timing, extraction, and activity evidence from a clean checkout.

## M5 — First silicon

- Small MPW test chip, not a full LLM.
- On-chip vectors, signature checking, observability, and fault diagnosis.
- Frequency/voltage sweep, shmoo plots, thermal and power measurement.
- Correlate RTL, post-layout estimates, and lab measurements.

**Exit criterion:** fabricated silicon executes a compiled matrix exactly and publishes honest
energy/operation and throughput data.

## M6 — Transformer subsystem

- Pipeline one useful decoder block or fixed-model subsystem.
- Integrate nonlinear, normalization, control, and stateful operators.
- Define KV-cache and host/chiplet interfaces.
- Evaluate residual programmability and model-update economics.

## M7 — Advanced-node qualification

- Obtain legitimate foundry/Europractice/university access and NDA coverage.
- Port only after the open-node methodology remains reliable.
- Rebuild cost models with real libraries, RC, variation, IR drop, EM, thermal, aging, yield, and
  reticle limits.
- Seek a shuttle or industrial partnership on evidence, not headline throughput claims.
