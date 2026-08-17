# Contributing

Hephaestus is pre-alpha research software. Contributions should improve evidence, correctness, or
reproducibility before expanding claims.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
hephaestus compile examples/tiny_weights.json --out build/tiny --verify-samples 256
./scripts/check_rtl.sh build/tiny/tiny_weights.sv tiny_weights
```

## Pull requests

A pull request should include:

- the problem and the evidence stage affected;
- tests for semantic changes;
- before/after structural or physical numbers for optimizations;
- exact commands and tool versions;
- claim limitations and known regressions;
- no proprietary PDK, library, checkpoint, or confidential material.

Do not optimize only the sample matrix and describe the result as general. New backends should have
matched baselines and deterministic artifacts.

## Generated files

Build outputs belong under `build/` and are not committed by default. Small curated benchmark
evidence may be committed under `benchmarks/` when its source/license and generation command are
fully documented.
