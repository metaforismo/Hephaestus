# Foundry and tapeout path

## Do not make 7 nm the first proof

An advanced FinFET node is useful only after the architecture survives synthesis, routing,
verification, and measurement. Starting there makes every compiler bug, interface mistake,
congestion problem, and verification gap expensive and hidden behind NDA tooling.

The recommended sequence is:

1. compile and verify tiny matrices in generic RTL;
2. synthesize equivalent baselines;
3. place and route a clocked tile on an open PDK;
4. close DRC/LVS/STA and run PEX;
5. fabricate a small MPW test chip;
6. use measured evidence to obtain an advanced-node partner.

## Practical European paths as of August 2026

### IHP open silicon

IHP publishes an SG13G2 open PDK and a LibreLane/OpenROAD path capable of assembling a full chip,
including pad ring, place-and-route, DRC, extraction, and LVS. Its 2026 open-silicon program lists
low-cost SG13G2 MPW access and a roughly six-month processing time. The exact schedule, collective
minimum area, price, packaging, and eligibility must be reconfirmed before committing funds.

This is the preferred Hephaestus v1 target because it can produce public, reproducible physical
evidence without putting a proprietary PDK in Git.

### Standard IHP MPW

IHP also lists normal 2026 SG13G2 MPW pricing, minimum-area conditions for selected runs, and a
BEOL-only option. This can be relevant if the open-silicon collective run does not fill or if the
design must remain private.

### TSMC through Europractice/imec

Europractice states that universities can request TSMC 16 nm and 7 nm access through the
University FinFET Program. Companies and research institutes can seek TSMC approval for nodes
including 7 nm and 5 nm, followed by a three-way TSMC–imec–customer NDA.

This is an access route, not a guarantee of affordable masks, reticle area, IP availability,
packaging, or shuttle acceptance. A serious request should include an institution/company,
funding, architecture evidence, expected area, interfaces, verification plan, and named design
engineers.

## What the first test chip should contain

- one or more small compiled mat-vec tiles;
- a programmable reference/bypass path;
- deterministic on-chip vectors and signatures;
- scan/DFT hooks;
- clock divider and voltage/frequency sweep support;
- activity counters and a measurement-friendly power domain;
- enough observability to distinguish arithmetic, routing, and I/O failures.

A full transformer is the wrong first die. The useful result is a trustworthy energy and timing
curve for a topology-compiled matrix against matched baselines.

## Public references

- Europractice TSMC access: https://europractice-ic.com/technologies/asics/tsmc/access-contacts/
- IHP MPW schedule and prices:
  https://www.ihp-microelectronics.com/services/research-and-prototyping-service/mpw-prototyping-service/schedule-price-list
- IHP open-silicon registration: https://dk.ihp-microelectronics.com/OpenSourceRequest.php
- IHP full-chip LibreLane flow:
  https://ihp-open-pdk-docs.readthedocs.io/en/latest/digital/librelane_full_chip.html
