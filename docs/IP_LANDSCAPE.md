# Taalas patent landscape: technical reading

This document is an engineering map, not legal advice or a freedom-to-operate opinion. Patent
claims must be interpreted by qualified counsel in each jurisdiction, including continuations,
family members, prosecution history, ownership changes, and prior art.

## Public families reviewed

### US 2025/0123802 A1 — configurable connectivity mesh

The application describes a set of fixed-value multipliers, a configurable connectivity mesh, and
readable cells associated one-to-one with parameters. The mesh can be configured through switches,
dopants, fuses, or customized wiring layers; high-level metal masks can specialize the circuit for
a model. Outputs are read and accumulated.

This is broader than “a normal ROM containing weights.” The public description combines fixed
value generation, model-specific connectivity, readable cells, and matrix-vector execution.

### WO 2026/015986 A1 — hardware implemented codebook pointers

This family describes a large data structure represented by hardware pointers or readable cells
that select a much smaller programmable set of values. Changing the registers can update the
represented model without rebuilding every pointer.

### US 2025/0238726 A1 — fixed model core plus fine-tuning portion

This application describes a less-configurable or hardwired base model together with a more
programmable fine-tuning portion, including examples using mask ROM and LoRA-like parameters.

### WO 2025/017481 A1 — integrated RAM using inverter loops

This family concerns an integrated memory architecture. It matters to a full system landscape but
is less central to the direct shift/add compiler implemented here.

## Implications for Hephaestus

The public thread’s phrase “Taalas still reads bits off a ROM” is too compressed to use as an
architecture conclusion. Public Taalas filings already discuss model-specific connectivity,
hardwired computation, readable cells, codebooks, and configurable residual/fine-tuning data.

Hephaestus therefore begins from a separately articulated primitive:

```text
compile a constant matrix into a direct arithmetic DAG whose runtime dataflow contains
activation values and partial sums, not addressed coefficient cells
```

Possible differentiating research axes include:

- direct constant-matrix adder graphs rather than readable parameter cells;
- algebraic factorization and cross-output common-subexpression sharing;
- quantization optimized jointly with post-route cost;
- structured transforms and sparse topology;
- a clearly separated programmable residual plane;
- open, reproducible compiler and verification infrastructure.

None of these bullets is a legal conclusion. Before commercial tapeout, commission a formal
claim chart and prior-art search, preserve independent-development records, and review every
backend—not only the current Python source.

## Primary public documents

- US20250123802A1, *Large Parameter Set Computation Accelerator Using Configurable Connectivity
  Mesh*: https://patents.google.com/patent/US20250123802A1/en
- WO2026015986A1, *Hardware Implemented Codebook Pointers*:
  https://patents.google.com/patent/WO2026015986A1/en
- US20250238726A1, *Computing Architecture with Model Core and Fine-Tuning Portion*:
  https://patents.google.com/patent/US20250238726A1/en
- WO2025017481A1, *Integrated Random Access Memory Using Inverter Loops*:
  https://patents.google.com/patent/WO2025017481A1/en
