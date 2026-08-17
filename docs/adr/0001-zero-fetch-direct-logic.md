# ADR 0001: Direct logic as the first backend

- Status: accepted
- Date: 2026-08-17

## Context

Model-specific inference can encode weights through storage, selectors, connectivity, arithmetic
logic, or combinations of them. Hephaestus needs one inspectable backend that establishes semantic
correctness and evidence discipline before broader architecture exploration.

## Decision

The first backend lowers each nonzero quantized coefficient to a signed shift of an activation and
represents each output as a shared addition DAG. It contains no runtime weight-storage object.

## Consequences

- The zero-runtime-weight-fetch property is structurally inspectable.
- Bit-exact integer equivalence is straightforward to test.
- Power-of-two codebooks limit numerical flexibility.
- Routing and fanout may dominate at scale.
- This backend does not imply that direct logic is always superior.
- Other architecture families remain explicit future backends rather than silent changes in meaning.
