# Post-physical sequential-equivalence research

This directory contains a research probe, not a production evidence layer.

The probe consumes the exact routed Verilog artifacts from the matched IHP SG13G2 physical bundle and compares them against the registered source contract. Its bounded proof currently uses four symbolic clock steps:

```text
step 1: reset asserted
step 2: reset released and first input accepted
step 3: valid pipeline advances
step 4: registered output is observable
```

Four steps therefore cover reset plus the declared one-cycle value and valid boundary. The SAT state starts at zero and reset remains explicit; arbitrary-power-up recovery is not inferred from this bounded run.

A separate temporal-induction attempt is recorded independently. Failure or timeout of that exploratory induction must not be misreported as a successful unbounded proof, and success of the bounded run alone does not enable a production post-physical-equivalence or comparative-PPA claim.

The miter always compares `valid_out`. Data is compared only when both implementations declare the result valid. Three negative controls must remain effective:

```text
data-dependent output corruption
valid-pipeline corruption
reset held active on the routed side
```

Timing behavior, X/Z semantics, SDF delays, independent DRC/LVS, activity power, PEX, foundry sign-off, and silicon remain outside this probe.