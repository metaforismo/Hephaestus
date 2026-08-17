# Exhaustive combinational equivalence

Randomized simulation is useful, but a finite test set cannot prove that two combinational RTL
implementations agree for every possible input. Hephaestus therefore adds a bounded formal evidence
level based on the Yosys SAT engine:

```text
yosys_sat_combinational_equivalence
```

## Independent reference

The formal runner does not use the shared-adder compilation plan as its oracle. It reads the
quantized `codes.npy` matrix, checks its SHA-256 against the verified matched-baseline manifest,
and independently generates a behavioral arithmetic reference.

For each output row, the reference:

1. sign-extends every input lane to the declared accumulator width;
2. multiplies each lane by the exact integer code;
3. accumulates the products using the same no-overflow integer contract;
4. exposes the result before external power-of-two row scaling.

This separates a lowering error from the oracle used to detect it.

## Miter and proof

For every matched backend, Hephaestus builds a one-bit mismatch miter:

```text
mismatch = |(backend_output XOR reference_output)
```

Yosys then proves:

```text
mismatch = 0
```

under `-set-def-inputs`. For a 48-bit input bus, this symbolically covers all \(2^{48}\) defined
bit patterns. It does not enumerate those vectors one by one.

The bundled command is:

```bash
python -m hephaestus.formal build/formal/matched \
  --codes build/formal/source/codes.npy \
  --out build/formal/evidence \
  --max-input-bits 64 \
  --timeout 300
```

The default input-width limit prevents an accidental unbounded formal job. Larger proofs must be
requested explicitly and may require partitioning, assumptions, or a stronger formal flow.

## Negative control

A proof harness is not trustworthy merely because it prints success. Hephaestus therefore runs a
negative control after the positive proofs. It modifies one backend observation so output bit zero
is XORed with input bit zero. Yosys must find a satisfying counterexample.

The evidence build fails when either condition is violated:

- a valid backend does not prove equivalent;
- the intentionally faulted negative control does not produce a model.

This checks that the miter is connected, the property is meaningful, and counterexamples are not
silently discarded.

## Artifact layout

```text
build/formal/evidence/
├── formal_evidence.json
├── source_matched_manifest.json
├── source_codes.npy
├── reference.sv
├── shared_dag/
│   ├── dut.sv
│   ├── reference.sv
│   ├── miter.sv
│   ├── proof.ys
│   ├── yosys.stdout.txt
│   └── yosys.stderr.txt
├── naive_shift_add/
│   └── ...
├── constant_multipliers/
│   └── ...
└── negative_control/
    └── ...
```

Every preserved input, script, miter, reference, and log receives a SHA-256 digest in the formal
manifest.

## What is proved

For the declared bounded combinational block, a passing positive result proves that the backend and
the independent integer reference produce identical output bits for every defined input bit-vector.
The result covers the exact signed widths, accumulator semantics, and pre-row-scaling code matrix
recorded by the matched contract.

## What is not proved

This evidence does not prove:

- floating-point model fidelity or downstream language-model quality;
- sequential or pipelined equivalence;
- behavior involving X/Z or analog electrical states;
- standard-cell mapping, timing closure, area, dynamic power, or leakage;
- place-and-route correctness, DRC/LVS, extracted parasitics, or PEX;
- clock, reset, scan, memory, KV-cache, package, or host-interface behavior;
- fabricated-silicon correctness.

Those remain separate evidence levels. Formal semantic correctness is necessary before physical
optimization, but it is not a substitute for physical evidence.
