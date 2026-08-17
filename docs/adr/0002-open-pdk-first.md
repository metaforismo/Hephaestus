# ADR 0002: Open/accessibile PDK evidence before advanced-node development

- Status: accepted
- Date: 2026-08-17

## Context

Advanced nodes offer density and performance but require approval, NDAs, expensive tool/library
access, and substantially higher tapeout risk. Early architectural uncertainty is dominated by
sharing, fanout, routing, and verification rather than final process scaling.

## Decision

Develop process-independent RTL and evidence first, then use an open or accessible production PDK
for routed experiments and a small MPW vehicle. Port to N7/N16 only after a measured or at least
DRC/LVS/PEX-clean tile exists.

## Consequences

- Public CI and reproducibility remain possible.
- Early silicon can be much smaller and cheaper.
- Absolute performance will not represent a leading node.
- Scaling projections must be labeled as projections.
- Private foundry backends must remain cleanly separated from the public compiler.
