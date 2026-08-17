# Generic Yosys synthesis evidence

The matched RTL bundle establishes a common arithmetic contract. The next evidence level runs the
same transparent Yosys flow for every backend and preserves enough information to reproduce and
audit the transformation.

This level is named:

```text
generic_yosys_post_techmap
```

It is deliberately not called PPA. No standard-cell library, clock constraint, physical parasitic,
placement, routing, activity trace, voltage, or process corner exists at this stage.

## Run the flow

```bash
python -m hephaestus.synthesis build/matched/backends \
  --out build/synthesis/evidence \
  --verify-repeatability
```

The input matched manifest must already report a verified integer contract. The command also
checks that each RTL file still matches the SHA-256 recorded by the matched bundle.

## Pinned generic flow

Each backend receives a private working directory containing a copied `input.sv` and the exact
Yosys script:

```text
read_verilog -sv
hierarchy -check -top <module>
proc
opt
flatten
opt
check
stat
write_json
techmap
opt
clean -purge
check
stat
write_json
```

Evidence is captured both before and after generic `techmap`. This preserves the distinction
between source-level arithmetic operators such as `$add` or `$mul` and the generic internal cells
to which Yosys lowers them.

## Artifact layout

```text
build/synthesis/evidence/
├── synthesis_evidence.json
├── shared_dag/
│   ├── input.sv
│   ├── synthesis.ys
│   ├── yosys.stdout.txt
│   ├── yosys.stderr.txt
│   ├── pre_techmap.stat.txt
│   ├── pre_techmap.netlist.json
│   ├── post_techmap.stat.txt
│   └── post_techmap.netlist.json
├── naive_shift_add/
│   └── ...
└── constant_multipliers/
    └── ...
```

For every stage, Hephaestus normalizes:

- total cells and distinct cell types;
- full cell-type histogram;
- abstract operator and generic internal-cell counts;
- input, output, and inout bit counts;
- net names and unique signal bits;
- cell connection bits;
- memories.

The manifest also records the Yosys version and SHA-256 for the input RTL, script, raw reports,
stdout/stderr, and both JSON netlists.

## Repeatability gate

With `--verify-repeatability`, each backend is synthesized a second time in a fresh temporary
directory. Both pre-techmap and post-techmap JSON netlists must be byte-identical, and all
normalized metrics must match. A mismatch fails the evidence build instead of being hidden.

This proves deterministic behavior for the specific Yosys executable and input. It does not claim
that different Yosys releases will emit the same netlist.

## Claim boundary

A successful run may claim:

```json
{
  "matched_integer_contract_verified": true,
  "generic_yosys_synthesis_completed": true
}
```

It must still report:

```json
{
  "standard_cell_mapping_performed": false,
  "timing_constrained": false,
  "post_synthesis_ppa_measured": false,
  "post_layout_pex_verified": false,
  "silicon_verified": false
}
```

The next evidence level must introduce a pinned liberty library, explicit timing constraints,
matched synthesis settings, and mapped reports. Only later can placement, routing, extraction, and
switching-based power answer whether the shared topology survives physical implementation.
