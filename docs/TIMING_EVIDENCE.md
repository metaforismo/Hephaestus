# Matched OpenSTA combinational timing evidence

This evidence layer sits after standard-cell mapping and mapped-netlist formal equivalence:

```text
verified integer contract
  → matched RTL
  → IHP SG13G2 standard-cell mapping
  → mapped-netlist SAT equivalence
  → matched OpenSTA combinational timing
```

It does not time unverified netlists.

## Common timing contract

The versioned contract is:

```text
configs/timing/ihp_sg13g2_combinational_typ_v1.json
```

Every backend uses the same IHP SG13G2 typical Liberty, virtual clock, zero external input/output
delay, 0.1 ns input transition, 0.05 pF output load, maximum path type, path count, and report
precision.

The 1000 ns virtual period is a reporting frame, not a frequency target. The primary comparison is
`worst_data_arrival_ns` under the declared library and boundary conditions. Hephaestus does not
convert that number into product Fmax.

## Fail-closed prerequisites

Before OpenSTA runs, the builder requires:

- successful matched integer-contract verification;
- successful IHP standard-cell mapping;
- successful mapped structural checks;
- successful exhaustive mapped-netlist equivalence;
- successful negative-control counterexample detection;
- matching mapped-manifest and netlist digests;
- matching technology ID, technology configuration, and Liberty digest.

It also rejects missing reports, OpenSTA errors, unconstrained paths, unsafe paths or identifiers,
missing arrival times, and non-repeatable normalized results when repeatability is requested.

## Reproduction

```bash
python -m hephaestus.timing build/timing/mapped \
  --mapped-formal build/timing/mapped-formal \
  --contract configs/timing/ihp_sg13g2_combinational_typ_v1.json \
  --out build/timing/opensta \
  --verify-repeatability \
  --timeout 300
```

The output preserves the input netlist, exact Tcl script, stdout, stderr, technology collateral,
source manifests, normalized paths, warnings, and SHA-256 provenance for each backend.

## Claim boundary

This layer can claim that formally verified standard-cell netlists were analyzed by OpenSTA under
one explicit combinational constraint contract and that constrained maximum path arrival values
were produced without unconstrained paths.

It cannot claim:

- clocked or sequential timing closure;
- post-placement or post-route timing;
- wire or extracted RC delay;
- crosstalk, variation, IR drop, or electromigration closure;
- dynamic or leakage power;
- post-layout PPA;
- DRC, LVS, PEX, or silicon behavior.

Those fields remain false in `timing_evidence.json`.
