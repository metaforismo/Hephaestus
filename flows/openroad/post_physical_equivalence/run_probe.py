"""Research post-physical sequential-equivalence probe.

This module consumes the bound OpenROAD evidence bundle, validates every source
and routed-netlist digest, builds an independent registered arithmetic contract,
and asks Yosys SAT to compare the routed netlists against that contract.

The first research level intentionally separates:
- bounded reset-recovery proof from arbitrary initial state;
- steady-state temporal induction from a zero-initialized state;
- three fault-injection negative controls.

A successful bounded proof is not described as foundry sign-off, four-state
equivalence, timing equivalence, or silicon verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SUCCESS_MARKER = "SAT proof finished - no model found: SUCCESS!"
_FAILURE_MARKER = "SAT proof finished - model found: FAIL!"


class PostPhysicalError(RuntimeError):
    """Raised when routed equivalence evidence cannot be built safely."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostPhysicalError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PostPhysicalError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PostPhysicalError(f"{context} is not a lowercase SHA-256 digest")
    return value


def _safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise PostPhysicalError(f"{context} is not a safe module name: {value!r}")
    return value


def _resolve_under(root: Path, relative: str, *, context: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise PostPhysicalError(f"{context} must be a relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / raw).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PostPhysicalError(f"{context} escapes the evidence root") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise PostPhysicalError(f"{context} is not a regular file: {resolved}")
    return resolved


def _validate_physical_evidence(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    evidence_path = root / "evidence" / "openroad_physical_evidence.json"
    prepared_path = root / "prepared" / "prepared.json"
    evidence = _load_json(evidence_path)
    prepared = _load_json(prepared_path)

    if evidence.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise PostPhysicalError("unsupported physical evidence schema")
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise PostPhysicalError("unsupported prepared evidence schema")

    claims = evidence.get("claims")
    required_true = (
        "registered_source_binding_verified",
        "pinned_orfs_image_used",
        "all_three_backends_placed",
        "all_three_backends_routed",
        "all_three_backends_emitted_gds",
        "all_three_backends_emitted_spef",
        "two_attempts_per_backend_completed",
        "physical_repeatability_verified",
        "common_physical_boundary_verified",
    )
    if not isinstance(claims, dict) or any(claims.get(name) is not True for name in required_true):
        raise PostPhysicalError("physical evidence prerequisites are incomplete")
    if claims.get("post_physical_equivalence_verified") is not False:
        raise PostPhysicalError(
            "source physical evidence unexpectedly claims downstream equivalence"
        )
    if set(evidence.get("backends", {})) != set(_BACKENDS):
        raise PostPhysicalError("physical evidence backend set is not exact")
    if set(prepared.get("backends", {})) != set(_BACKENDS):
        raise PostPhysicalError("prepared evidence backend set is not exact")

    expected_prepared = _require_digest(
        evidence.get("source", {}).get("prepared_manifest_sha256"),
        context="physical source prepared manifest",
    )
    if _sha256(prepared_path) != expected_prepared:
        raise PostPhysicalError("prepared manifest digest differs from physical evidence")

    reference_core = root / "prepared" / "registered" / "reference_core.sv"
    if not reference_core.is_file():
        raise PostPhysicalError("prepared registered reference_core.sv is missing")
    return evidence, prepared, reference_core


def _extract_module_name(source: str, *, context: str) -> str:
    matches = re.findall(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(", source)
    if len(matches) != 1:
        raise PostPhysicalError(
            f"{context} must contain exactly one module declaration; found {matches}"
        )
    return _safe_module(matches[0], context=context)


def emit_registered_contract(
    *,
    reference_core_module: str,
    module_name: str,
    input_bits: int,
    output_bits: int,
) -> str:
    """Emit the registered contract independently of the backend wrapper RTL."""

    core = _safe_module(reference_core_module, context="reference core module")
    module = _safe_module(module_name, context="registered contract module")
    if input_bits <= 0 or output_bits <= 0:
        raise ValueError("contract bus widths must be positive")
    return f"""// Independent registered contract around the arithmetic reference core.
module {module} (
    input  wire clk,
    input  wire reset,
    input  wire valid_in,
    input  wire signed [{input_bits - 1}:0] x_flat,
    output reg valid_out,
    output reg signed [{output_bits - 1}:0] y_flat
);
  reg signed [{input_bits - 1}:0] x_q;
  reg valid_q;
  wire signed [{output_bits - 1}:0] y_comb;

  {core} arithmetic_reference (
      .x_flat(x_q),
      .y_flat(y_comb)
  );

  always @(posedge clk) begin
    if (reset) begin
      x_q <= {input_bits}'sd0;
      valid_q <= 1'b0;
      valid_out <= 1'b0;
      y_flat <= {output_bits}'sd0;
    end else begin
      x_q <= x_flat;
      valid_q <= valid_in;
      valid_out <= valid_q;
      y_flat <= y_comb;
    end
  end
endmodule
"""


def emit_miter(
    *,
    dut_module: str,
    reference_module: str,
    module_name: str,
    input_bits: int,
    output_bits: int,
    fault: str = "none",
) -> str:
    """Emit a sequential miter with one explicit fault-injection mode."""

    dut = _safe_module(dut_module, context="routed DUT module")
    reference = _safe_module(reference_module, context="reference module")
    module = _safe_module(module_name, context="miter module")
    if fault not in {"none", "data", "valid", "reset"}:
        raise ValueError(f"unsupported fault mode: {fault}")

    dut_reset = "1'b0" if fault == "reset" else "reset"
    lines = [
        f"module {module} (",
        "    input  wire clk,",
        "    input  wire reset,",
        "    input  wire valid_in,",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        "    output wire mismatch",
        ");",
        "  wire dut_valid;",
        f"  wire signed [{output_bits - 1}:0] dut_y;",
        "  wire reference_valid;",
        f"  wire signed [{output_bits - 1}:0] reference_y;",
        f"  {dut} routed (",
        "      .clk(clk),",
        f"      .reset({dut_reset}),",
        "      .valid_in(valid_in),",
        "      .x_flat(x_flat),",
        "      .valid_out(dut_valid),",
        "      .y_flat(dut_y)",
        "  );",
        f"  {reference} reference (",
        "      .clk(clk),",
        "      .reset(reset),",
        "      .valid_in(valid_in),",
        "      .x_flat(x_flat),",
        "      .valid_out(reference_valid),",
        "      .y_flat(reference_y)",
        "  );",
    ]
    observed_valid = "dut_valid"
    observed_y = "dut_y"
    if fault == "valid":
        lines.extend(
            [
                "  wire faulted_valid;",
                "  assign faulted_valid = dut_valid ^ valid_in;",
            ]
        )
        observed_valid = "faulted_valid"
    elif fault == "data":
        lines.extend(
            [
                f"  wire [{output_bits - 1}:0] data_fault_mask;",
                f"  wire signed [{output_bits - 1}:0] faulted_y;",
                (
                    f"  assign data_fault_mask = "
                    f"{{{{{output_bits - 1}{{1'b0}}}}, "
                    "(dut_valid & x_flat[0])};"
                ),
                "  assign faulted_y = dut_y ^ data_fault_mask;",
            ]
        )
        observed_y = "faulted_y"

    lines.extend(
        [
            f"  wire valid_mismatch = {observed_valid} ^ reference_valid;",
            f"  wire data_mismatch = |({observed_y} ^ reference_y);",
            "  assign mismatch = valid_mismatch | data_mismatch;",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def _reset_constraints(cycles: int) -> list[str]:
    if cycles < 3:
        raise ValueError("bounded proof requires at least three cycles")
    commands = ["-set-at 1 reset 1"]
    commands.extend(f"-set-at {step} reset 0" for step in range(2, cycles + 1))
    return commands


def emit_bounded_script(*, top: str, cycles: int, expect_counterexample: bool) -> str:
    """Emit bounded reset-recovery proof commands."""

    module = _safe_module(top, context="bounded miter top")
    sat = [
        "sat",
        f"-seq {cycles}",
        "-set-def-inputs",
        "-set-init-def",
        "-prove-skip 1",
        "-prove mismatch 0",
        "-show-inputs",
        "-show-outputs",
        *_reset_constraints(cycles),
    ]
    if not expect_counterexample:
        sat.insert(1, "-verify")
    return "\n".join(
        [
            "read_verilog -sv models.v routed.v reference_core.sv contract.sv miter.sv",
            f"hierarchy -check -top {module}",
            "proc",
            "async2sync",
            "flatten",
            "opt",
            "check",
            " ".join(sat),
            "",
        ]
    )


def emit_inductive_script(*, top: str, maxsteps: int) -> str:
    """Emit zero-initialized, reset-low steady-state temporal induction."""

    module = _safe_module(top, context="inductive miter top")
    if maxsteps <= 0:
        raise ValueError("maxsteps must be positive")
    return "\n".join(
        [
            "read_verilog -sv models.v routed.v reference_core.sv contract.sv miter.sv",
            f"hierarchy -check -top {module}",
            "proc",
            "async2sync",
            "flatten",
            "opt",
            "check",
            (
                "sat -verify -tempinduct -seq 2 "
                f"-maxsteps {maxsteps} -set-def-inputs -set-init-zero "
                "-set reset 0 -prove mismatch 0 -show-inputs -show-outputs"
            ),
            "",
        ]
    )


def _run_yosys(
    *,
    yosys: str,
    workdir: Path,
    script_name: str,
    timeout: int,
    expect_counterexample: bool,
) -> dict[str, Any]:
    completed = subprocess.run(
        [yosys, "-s", script_name],
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout_path = workdir / f"{Path(script_name).stem}.stdout.txt"
    stderr_path = workdir / f"{Path(script_name).stem}.stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    combined = completed.stdout + "\n" + completed.stderr
    success = _SUCCESS_MARKER in combined
    counterexample = _FAILURE_MARKER in combined
    if expect_counterexample:
        passed = completed.returncode == 0 and counterexample and not success
    else:
        passed = completed.returncode == 0 and success and not counterexample
    return {
        "performed": True,
        "passed": passed,
        "expected_counterexample": expect_counterexample,
        "returncode": completed.returncode,
        "proof_success": success,
        "counterexample_found": counterexample,
        "stdout": stdout_path.name,
        "stdout_sha256": _sha256(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": _sha256(stderr_path),
    }


def _copy_and_verify(
    source: Path,
    destination: Path,
    expected_digest: str,
    *,
    context: str,
) -> None:
    if _sha256(source) != _require_digest(expected_digest, context=context):
        raise PostPhysicalError(f"{context} source digest mismatch")
    shutil.copyfile(source, destination)
    if _sha256(destination) != expected_digest:
        raise PostPhysicalError(f"{context} copied digest mismatch")


def build_probe(
    physical_root: Path,
    models_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    cycles: int = 8,
    maxsteps: int = 12,
    timeout: int = 300,
) -> dict[str, Any]:
    """Build and execute bounded and inductive routed equivalence probes."""

    root = physical_root.resolve()
    output = output_dir.resolve()
    models = models_path.resolve()
    if not models.is_file() or models.is_symlink():
        raise PostPhysicalError(f"formal cell models are missing: {models}")
    if shutil.which(yosys) is None and not Path(yosys).is_file():
        raise PostPhysicalError(f"Yosys executable was not found: {yosys}")

    physical, prepared, reference_core = _validate_physical_evidence(root)
    contract = prepared.get("contract", {}).get("value")
    if not isinstance(contract, dict):
        raise PostPhysicalError("prepared physical contract is malformed")
    registered_manifest = _load_json(root / "prepared" / "registered" / "registered_manifest.json")
    registered_contract = registered_manifest.get("contract")
    if not isinstance(registered_contract, dict):
        raise PostPhysicalError("registered contract is malformed")
    input_bits = int(registered_contract.get("input_bits", 0))
    output_bits = int(registered_contract.get("output_bits", 0))
    if input_bits != 48 or output_bits != 48:
        raise PostPhysicalError(
            f"research probe currently requires the pinned 48×48 contract, got "
            f"{input_bits}×{output_bits}"
        )
    reference_text = reference_core.read_text(encoding="utf-8")
    reference_core_module = _extract_module_name(
        reference_text,
        context="reference_core.sv",
    )

    output.mkdir(parents=True, exist_ok=True)
    source_physical = output / "source_openroad_physical_evidence.json"
    source_prepared = output / "source_prepared.json"
    source_registered = output / "source_registered_manifest.json"
    shutil.copyfile(root / "evidence" / "openroad_physical_evidence.json", source_physical)
    shutil.copyfile(root / "prepared" / "prepared.json", source_prepared)
    shutil.copyfile(
        root / "prepared" / "registered" / "registered_manifest.json",
        source_registered,
    )

    backends: dict[str, Any] = {}
    all_positive_bounded = True
    all_positive_inductive = True
    all_negative_controls = True

    for backend_name in _BACKENDS:
        backend_physical = physical["backends"][backend_name]
        backend_prepared = prepared["backends"][backend_name]
        runs = backend_physical.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise PostPhysicalError(f"{backend_name} does not contain two physical runs")
        run = next((item for item in runs if item.get("attempt") == 1), None)
        if not isinstance(run, dict):
            raise PostPhysicalError(f"{backend_name} attempt 1 is missing")
        final_verilog = run.get("artifacts", {}).get("final_verilog")
        if not isinstance(final_verilog, dict):
            raise PostPhysicalError(f"{backend_name} routed Verilog metadata is missing")
        routed_digest = _require_digest(
            final_verilog.get("sha256"),
            context=f"{backend_name}.final_verilog",
        )
        attempt_root = root / "downloaded-runs" / f"openroad-physical-run-{backend_name}-1"
        routed_source = _resolve_under(
            attempt_root,
            str(final_verilog.get("path")),
            context=f"{backend_name}.final_verilog",
        )
        if _sha256(routed_source) != routed_digest:
            raise PostPhysicalError(
                f"{backend_name} routed Verilog digest differs from physical evidence"
            )
        routed_module = _safe_module(
            backend_prepared.get("wrapper_module"),
            context=f"{backend_name}.wrapper_module",
        )
        if (
            _extract_module_name(
                routed_source.read_text(encoding="utf-8"),
                context=f"{backend_name} routed Verilog",
            )
            != routed_module
        ):
            raise PostPhysicalError(f"{backend_name} routed top module differs")

        backend_dir = output / "backends" / backend_name
        backend_dir.mkdir(parents=True, exist_ok=True)
        _copy_and_verify(
            routed_source,
            backend_dir / "routed.v",
            routed_digest,
            context=f"{backend_name}.routed",
        )
        shutil.copyfile(reference_core, backend_dir / "reference_core.sv")
        shutil.copyfile(models, backend_dir / "models.v")

        contract_module = f"hephaestus_postphysical_{backend_name}_contract"
        (backend_dir / "contract.sv").write_text(
            emit_registered_contract(
                reference_core_module=reference_core_module,
                module_name=contract_module,
                input_bits=input_bits,
                output_bits=output_bits,
            ),
            encoding="utf-8",
        )

        positive_top = f"hephaestus_postphysical_{backend_name}_positive"
        (backend_dir / "miter.sv").write_text(
            emit_miter(
                dut_module=routed_module,
                reference_module=contract_module,
                module_name=positive_top,
                input_bits=input_bits,
                output_bits=output_bits,
            ),
            encoding="utf-8",
        )
        (backend_dir / "bounded.ys").write_text(
            emit_bounded_script(
                top=positive_top,
                cycles=cycles,
                expect_counterexample=False,
            ),
            encoding="utf-8",
        )
        bounded = _run_yosys(
            yosys=yosys,
            workdir=backend_dir,
            script_name="bounded.ys",
            timeout=timeout,
            expect_counterexample=False,
        )
        all_positive_bounded = all_positive_bounded and bounded["passed"]

        (backend_dir / "inductive.ys").write_text(
            emit_inductive_script(top=positive_top, maxsteps=maxsteps),
            encoding="utf-8",
        )
        inductive = _run_yosys(
            yosys=yosys,
            workdir=backend_dir,
            script_name="inductive.ys",
            timeout=timeout,
            expect_counterexample=False,
        )
        all_positive_inductive = all_positive_inductive and inductive["passed"]

        negatives: dict[str, Any] = {}
        for fault in ("data", "valid", "reset"):
            fault_dir = backend_dir / f"negative_{fault}"
            fault_dir.mkdir()
            for name in ("routed.v", "reference_core.sv", "contract.sv", "models.v"):
                shutil.copyfile(backend_dir / name, fault_dir / name)
            top = f"hephaestus_postphysical_{backend_name}_negative_{fault}"
            (fault_dir / "miter.sv").write_text(
                emit_miter(
                    dut_module=routed_module,
                    reference_module=contract_module,
                    module_name=top,
                    input_bits=input_bits,
                    output_bits=output_bits,
                    fault=fault,
                ),
                encoding="utf-8",
            )
            (fault_dir / "proof.ys").write_text(
                emit_bounded_script(
                    top=top,
                    cycles=cycles,
                    expect_counterexample=True,
                ),
                encoding="utf-8",
            )
            proof = _run_yosys(
                yosys=yosys,
                workdir=fault_dir,
                script_name="proof.ys",
                timeout=timeout,
                expect_counterexample=True,
            )
            negatives[fault] = proof
            all_negative_controls = all_negative_controls and proof["passed"]

        backends[backend_name] = {
            "routed_module": routed_module,
            "routed_verilog_sha256": routed_digest,
            "source_core_sha256": backend_prepared["core_sha256"],
            "source_wrapper_sha256": backend_prepared["wrapper_sha256"],
            "bounded_reset_recovery": bounded,
            "steady_state_temporal_induction": inductive,
            "negative_controls": negatives,
        }

    claims = {
        "physical_evidence_prerequisite_verified": True,
        "physical_repeatability_prerequisite_verified": True,
        "registered_contract_reconstructed_from_independent_reference": True,
        "bounded_reset_recovery_equivalence_verified": all_positive_bounded,
        "steady_state_temporal_induction_verified": all_positive_inductive,
        "data_fault_counterexample_found": all(
            value["negative_controls"]["data"]["passed"] for value in backends.values()
        ),
        "valid_latency_fault_counterexample_found": all(
            value["negative_controls"]["valid"]["passed"] for value in backends.values()
        ),
        "reset_fault_counterexample_found": all(
            value["negative_controls"]["reset"]["passed"] for value in backends.values()
        ),
        "post_physical_equivalence_verified": (
            all_positive_bounded and all_positive_inductive and all_negative_controls
        ),
        "comparative_ppa_claim_enabled": False,
        "four_state_semantics_verified": False,
        "timing_annotated_functional_semantics_verified": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }
    evidence = {
        "schema": "hephaestus.post-physical-equivalence-probe.v1",
        "evidence_level": ("bounded_reset_recovery_and_zero_init_temporal_induction_research"),
        "source": {
            "physical_evidence": source_physical.name,
            "physical_evidence_sha256": _sha256(source_physical),
            "prepared_manifest": source_prepared.name,
            "prepared_manifest_sha256": _sha256(source_prepared),
            "registered_manifest": source_registered.name,
            "registered_manifest_sha256": _sha256(source_registered),
            "reference_core_sha256": _sha256(reference_core),
            "functional_cell_models_sha256": _sha256(models),
        },
        "scope": {
            "backends": list(_BACKENDS),
            "input_bits": input_bits,
            "output_bits": output_bits,
            "bounded_cycles": cycles,
            "temporal_induction_maxsteps": maxsteps,
            "reset_sequence": [1] + [0] * (cycles - 1),
            "two_state_semantics": True,
            "zero_delay_functional_cell_models": True,
        },
        "tool": {
            "yosys": yosys,
        },
        "backends": backends,
        "claims": claims,
    }
    _write_json(output / "post_physical_equivalence_probe.json", evidence)
    if not all_positive_bounded:
        raise PostPhysicalError("one or more bounded positive proofs failed")
    if not all_negative_controls:
        raise PostPhysicalError("one or more negative controls failed to produce a counterexample")
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe routed registered netlists against an independent contract."
    )
    parser.add_argument("physical_root", type=Path)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/post-physical"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--maxsteps", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = build_probe(
            args.physical_root,
            args.models,
            args.out,
            yosys=args.yosys,
            cycles=args.cycles,
            maxsteps=args.maxsteps,
            timeout=args.timeout,
        )
    except (OSError, ValueError, PostPhysicalError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        "post-physical probe completed; "
        f"bounded={evidence['claims']['bounded_reset_recovery_equivalence_verified']} "
        f"inductive={evidence['claims']['steady_state_temporal_induction_verified']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
