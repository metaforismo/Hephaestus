# Research probe: official IHP KLayout DRC collateral

This branch is methodology research and is not intended to merge as-is.

The probe consumes the matched physical artifact generated for the exact branch
head and binds all six routed GDS files:

```text
3 matched backends × 2 isolated physical attempts = 6 GDS inputs
```

It checks out the IHP Open PDK at commit:

```text
22f2a25f1734796de3debbbf29cf697cbbc54081
```

The runner inventories every official `.lydrc` file and the PDK's DRC workflow
collateral, records SHA-256 provenance, discovers a working headless KLayout
invocation, and parses the resulting `.lyrdb` report database rather than
trusting the command exit status.

For each exact GDS it runs:

```text
positive: original digest-bound routed GDS
negative: copy with deterministic one-DBU rectangles inserted on populated layers
```

The negative control must produce a report outcome different from, and normally
strictly larger than, the corresponding positive report. The original GDS is
never modified.

A successful research artifact may establish only that:

- an official open IHP KLayout deck was found and executed;
- six exact physical inputs and their run manifests were bound;
- six positive and six negative report databases were parsed;
- deterministic invalid geometry was observable to that deck.

It must keep these false:

```text
open_minimal_drc_qualified
drc_clean
foundry_signoff_drc_clean
foundry_signoff_complete
silicon_verified
```

Even an empty positive report under an open deck would not be foundry sign-off.
Promotion requires a clean permanent package, pinned KLayout distribution,
explicit selected-deck contract, stable report parser, independently replayed
report databases, versioned regression predicates, exact-head CI, documentation,
and removal of this research workflow and branch.
