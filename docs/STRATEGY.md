# Strategy: what “better than hardwired weight ROM” should mean

The useful goal is not to reproduce a screenshot or maximize one headline number. It is to build
a compiler and architecture whose advantage survives quantization, synthesis, routing, timing,
power, model quality, manufacturing, and model updates.

## 1. The physical conservation law

An 8-billion-parameter model at 3 bits still carries 24 billion bits of model information. A
direct-logic implementation can eliminate runtime coefficient retrieval, but it cannot eliminate
the physical representation of that information. The information reappears as some combination
of devices, metal choices, fanout, capacitance, logic states, and masks.

Therefore:

```text
zero runtime weight fetch ≠ zero physical cost ≠ zero switching energy
```

The opportunity is to represent the model more efficiently than one independent stored symbol per
weight by exploiting repetition, sparsity, algebraic structure, and application-specific timing.

## 2. The compiler advantage

A strong Hephaestus compiler should search three spaces at once:

### Numerical space

- codebook values;
- scale granularity;
- mixed precision;
- outlier handling;
- pruning and structured sparsity;
- low-rank or transform factorization;
- calibration and task loss.

### Logical space

- signed-digit recoding;
- shared partial sums across outputs;
- Boolean/arithmetic factoring;
- pipeline boundaries;
- residual programmability;
- exact versus approximate subgraphs.

### Physical space

- cell area and drive strength;
- fanout and buffering;
- logic depth and slack;
- wire length and congestion;
- clock and power distribution;
- switching activity, glitches, IR drop, and thermal density.

Most quantizers optimize model error and then hand the result to hardware. Hephaestus should feed
post-synthesis and post-route cost back into quantization. That closed loop is the main software
moat.

## 3. The architecture advantage

The first direct-logic backend is only one point. The likely product architecture is hybrid:

```text
fixed topology base
  + small programmable outlier/residual plane
  + programmable nonlinear/stateful engine
  + external or chiplet KV-cache and host interface
```

This preserves most fixed-model efficiency while allowing adapters, safety patches, calibration,
and customer specialization. It also avoids fabricating an entirely new full die for every minor
model revision.

## 4. The manufacturing advantage

Do not begin by squeezing a full model under an advanced-node reticle. Begin with a tile whose
behavior and physical costs can be measured. A tile can answer the hard questions cheaply:

- Does common-subexpression sharing survive routing?
- Is wire energy larger than the avoided memory energy?
- Which codebook wins after placement rather than before synthesis?
- What pipeline depth maximizes energy efficiency?
- How much programmable residual is worth its area?
- How stable are results across PVT and real silicon?

Once those answers exist, scaling to chiplets or model-specific reticle-scale devices becomes an
engineering extrapolation rather than a speculative claim.

## 5. The initial win condition

The first credible public result is not 40,000 tokens/s. It is:

1. one published constant matrix and matched baselines;
2. identical I/O precision and timing constraints;
3. bit-exact or quantified approximate behavior;
4. DRC/LVS-clean post-layout implementations;
5. extracted timing and switching-based energy;
6. raw reports and a reproducible compiler invocation;
7. later, measured MPW silicon.

A consistent advantage there is enough to attract serious compiler researchers, chip designers,
universities, grants, foundry programs, and strategic partners.
