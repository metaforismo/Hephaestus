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

## M3.5 — Registered matched tile boundary — implemented for the regression microcase

- Bind each registered wrapper to an already verified matched RTL bundle.
- Require exhaustive combinational proofs and an effective formal negative control for the source
  cores before registration.
- Apply identical input registers, output registers, reset behavior, valid propagation, and bus
  widths to shared-DAG, naive shift/add, and constant-multiplier backends.
- Verify one-cycle latency and initiation interval one against an independent arithmetic oracle.
- Exercise continuous traffic, valid bubbles, reset clearing, and a data-dependent simulation
  negative control.
- Preserve a versioned registered evidence manifest and regression reference.

**Exit criterion met for the 4×6 microcase:** all three wrappers accept one transaction per cycle,
return it one cycle later, match the independent oracle across 272 valid vectors plus 39 bubbles,
and detect the injected negative-control fault. This is simulation evidence, not sequential formal
proof or physical timing closure.

## M4 — Open-PDK RTL-to-GDS — physical execution implemented, sign-off chain incomplete

Implemented for the 4×6 registered regression microcase:

- One immutable ORFS container digest and one common IHP SG13G2 physical contract.
- Three registered backends, each executed in two isolated attempts.
- Synthesis, floorplanning, placement, CTS, routing, GDS generation, and SPEF extraction.
- Digest binding for registered sources, physical run manifests, final GDS, DEF, Verilog, OpenDB,
  SPEF, tool banners, and selected physical metrics.
- Repeatability checks for stable routed artifacts and an effective source-binding negative control.

Still required before M4 is complete:

- A permanent same-run post-physical sequential-equivalence gate. The historical research harness
  is useful input, but its full routed-netlist bounded SAT run timed out and is not qualifying
  evidence.
- Independent DRC using a versioned open rule deck and an invalid-geometry negative control.
- LVS against the exact routed/schematic pair with both layout-side and schematic-side negative
  controls.
- Validated post-layout extraction and activity-based power under a versioned workload.
- Scan/DFT planning, physical I/O planning, and eventually a pad-ring wrapper.

**Current claim boundary:** matched placement/routing and declared two-run physical repeatability
are established for the exact regression case. Post-physical equivalence, comparative PPA,
independent DRC/LVS, activity power, validated PEX, foundry sign-off, and silicon remain unverified.

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
