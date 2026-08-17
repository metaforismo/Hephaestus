# Research map

## Quantization

- GPTQ: approximate second-order post-training quantization to 3/4 bits and extreme 2-bit or
  ternary regimes. https://arxiv.org/abs/2210.17323
- AWQ: activation-aware protection/scaling of salient weights with hardware-friendly low-bit
  weight quantization. https://proceedings.mlsys.org/paper_files/paper/2024/hash/42a452cbafa9dd64e9ba4aa95cc1ef21-Abstract-Conference.html
- AQLM: additive multi-codebook quantization for the sub-3-bit regime.
  https://arxiv.org/abs/2401.06118
- QuIP#: incoherence processing and hardware-efficient E8 lattice codebooks.
  https://arxiv.org/abs/2402.04396
- ShiftAddLLM: post-training multiplication-less reparameterization into binary matrices,
  shifts, and additions. https://arxiv.org/abs/2406.05981

## Constant-matrix and direct-logic synthesis

- Dempster and Macleod, multiplierless linear-transform realization using common-subexpression
  elimination and numerical methods. https://doi.org/10.1145/2071356.2071359
- Bilgili and Yurdakul, scalable CSE extraction/compression for sparse constant matrices.
  https://arxiv.org/abs/2303.16106
- LogicNets, direct truth-table netlists for sparse low-bit neural networks.
  https://arxiv.org/abs/2004.03021

## Accelerator context

- DianNao: memory-centric neural-network accelerator architecture.
- Cambricon-X and Cambricon-S: sparse neural-network accelerators and the hardware consequences of
  pruning granularity and indexing.
- Distributed arithmetic and multiple-constant multiplication literature: alternatives to
  coefficient-by-coefficient multipliers.

## Open EDA

- Yosys for RTL synthesis: https://yosyshq.net/yosys/
- OpenROAD Flow for RTL-to-GDS: https://openroad-flow-scripts.readthedocs.io/
- CIRCT/MLIR for a future typed hardware IR: https://circt.llvm.org/
- IHP SG13G2 Open PDK: https://github.com/IHP-GmbH/IHP-Open-PDK

## Open questions

1. Which quantization codebooks minimize routed energy rather than abstract multiply count?
2. When does direct logic lose to a codebook/distribution network because of wire capacitance?
3. How much sharing survives placement after fanout buffering and timing closure?
4. Can transform/factorized matrices reduce both model error and physical congestion?
5. What is the optimal size of a programmable residual/outlier plane?
6. How should a fixed model handle frequent fine-tunes, safety updates, or model replacement?
7. Is layer-by-layer fixed silicon better than one monolithic model die for yield and product life?
