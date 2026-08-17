# Benchmarking and claim discipline

A hardware result is useful only when its denominator and evidence stage are explicit.

## Required result levels

1. **Algorithmic estimate:** operation count, graph depth, sparsity, and an explained proxy.
2. **Post-synthesis:** mapped cells, area estimate, timing, and tool/library versions.
3. **Post-placement:** utilization, congestion, wire length, buffering, and timing.
4. **Post-route:** DRC status, extracted parasitics, STA corners, and switching-based power.
5. **Post-PEX waveform:** electrical simulation for a bounded block and stated vectors.
6. **FPGA:** board, clock, resource use, I/O assumptions, and measured wall power.
7. **Silicon:** process, die area, package, voltage, frequency, temperature, yield sample,
   instrumentation, and measured power.

Never present one level as another.

## Mat-vec benchmark record

Every benchmark directory should contain:

```text
manifest.json
source.sha256
quantization.json
accuracy.json
rtl/
synthesis/
place_route/
verification/
power/
README.md
```

At minimum record:

- source model and exact revision;
- tensor names and shapes;
- calibration/evaluation datasets and licenses;
- quantizer configuration and random seeds;
- baseline architecture;
- input/output precision and accumulation rules;
- batch, context, prompt/decode phase, and sequence lengths;
- clock, voltage, process corner, temperature, and utilization;
- whether weight, activation, KV, I/O, leakage, clock, and host power are included.

## Memory-fetch metric

“100× fewer memory fetches” is not reproducible until the event is defined. Possible counters are
SRAM word reads, ROM word-line activations, register-file accesses, cache-line fills, off-chip
transactions, or logical coefficient lookups. Hephaestus reports the structural counter:

```text
runtime coefficient retrieval operations from a weight storage object
```

For the direct-logic core it is zero. Energy still depends on activation movement, wire charging,
adder switching, clocking, and physical implementation.

## Throughput

Tokens/s must include the model, precision, batch, prompt length, generated length, sampling path,
host overhead, and whether it is per user or aggregate. Report latency separately. A fixed model
can have radically different prefill and decode bottlenecks.

## Baselines

The first fair baselines are:

- constant coefficients implemented by generic multipliers;
- a small codebook plus selectors/ROM;
- shift/add without global sharing;
- shift/add with sharing;
- fixed base plus programmable residual.

Use the same PDK, constraints, interface, pipeline depth, and verification vectors.
