# Structural evidence bundles

Hephaestus benchmark suites turn compiler runs into reviewable, reproducible artifacts. The first
runner deliberately stops at the algorithmic and RTL boundary; it does not convert operation
counts or generated HDL into claims about standard-cell area, timing, routed energy, PEX, token
throughput, or silicon.

## Run the bundled suite

```bash
python -m pip install -e '.[dev]'
python -m hephaestus.benchmark benchmarks/suites/tiny.json \
  --out build/evidence/tiny
```

The suite currently compiles an exact 4×6 example and a matrix constructed with repeated partial
sums. Each case must complete randomized bit-exact integer verification and report zero runtime
coefficient reads.

## Artifact layout

```text
build/evidence/tiny/
├── evidence.json
├── SUMMARY.md
├── tiny_exact/
│   ├── compile.stdout.txt
│   ├── compile.stderr.txt
│   ├── manifest.json
│   ├── plan.json
│   ├── codes.npy
│   └── hephaestus_tiny_exact.sv
└── shared_pairs/
    └── ...
```

`evidence.json` records the source SHA-256, hashes of the manifest, plan, quantized codes and RTL,
the compiler version, matrix density, naive and compiled adder counts, graph depth when available,
quantization error, and the explicit claim boundary.

## Failure policy

The runner fails closed when:

- a source or required artifact is missing;
- a suite uses an unsupported schema version;
- compilation fails;
- bit-exact integer-core verification is not reported;
- runtime coefficient reads are missing or nonzero;
- an artifact cannot be decoded safely.

It does not fail merely because an optimized design uses more additions than the transparent naive
baseline. Negative results remain in the evidence instead of being filtered out.

## Current claim boundary

Every structural suite keeps these fields false:

```json
{
  "post_synthesis_ppa_measured": false,
  "post_layout_pex_verified": false,
  "silicon_verified": false
}
```

A future synthesis or physical-design runner must use a separate evidence level and preserve the
raw tool reports, tool versions, constraints, libraries, hashes, and matched-baseline conditions.
