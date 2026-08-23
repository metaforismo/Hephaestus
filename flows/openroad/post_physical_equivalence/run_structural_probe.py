"""Research structural-equivalence probe for routed registered netlists.

The existing independent arithmetic miter is deliberately not reused here. The
registered source cores are already exhaustively proved and their wrappers are
bound to the physical evidence. This probe asks the narrower downstream
question: did RTL-to-GDS preserve each exact registered source implementation?
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

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
FAULTS = ("data", "valid", "reset")
SUCCESS_MARKER = "Equivalence successfully proven!"
UNPROVEN_MARKER = "unproven $equiv cells"
MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class StructuralProbeError(RuntimeError):
    """Raised when the structural probe cannot be assembled safely."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StructuralProbeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StructuralProbeError(f"JSON root must be an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or MODULE_RE.fullmatch(value) is None:
        raise StructuralProbeError(f"{context} is not a safe module name: {value!r}")
    return value


def exactly_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.rglob(pattern))
    if len(matches) != 1:
        raise StructuralProbeError(
            f"expected exactly one {pattern!r} under {root}, found {matches}"
        )
    path = matches[0]
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise StructuralProbeError(f"invalid probe input: {path}")
    return path


def emit_equivalence_script(*, source_top: str, gate_top: str) -> str:
    source = safe_module(source_top, context="source top")
    gate = safe_module(gate_top, context="gate top")
    commands = [
        "# Normalize the exact registered source.",
        "read_verilog -sv source_core.sv source_wrapper.sv",
        f"hierarchy -check -top {source}",
        "proc",
        "async2sync",
        "flatten",
        "memory",
        "opt -full",
        f"rename {source} gold",
        "design -stash gold_design",
        "",
        "# Normalize the routed netlist with functional-only IHP cell models.",
        "read_verilog -sv models.v routed.v fault_wrapper.sv",
        f"hierarchy -check -top {gate}",
        "proc",
        "async2sync",
        "flatten",
        "memory",
        "opt -full",
        f"rename {gate} gate",
        "design -stash gate_design",
        "",
        "# Import both normalized tops into one equivalence design.",
        "design -copy-from gold_design gold",
        "design -copy-from gate_design gate",
        "equiv_make gold gate equiv",
        "hierarchy -check -top equiv",
        "proc",
        "opt -full",
        "equiv_struct -maxiter 20",
        "equiv_simple",
        "equiv_induct -seq 4",
        "equiv_status -assert",
        "",
    ]
    return "\n".join(commands)


def emit_passthrough_wrapper(
    *,
    routed_top: str,
    wrapper_top: str,
    input_bits: int,
    output_bits: int,
) -> str:
    routed = safe_module(routed_top, context="routed top")
    wrapper = safe_module(wrapper_top, context="passthrough wrapper")
    return "\n".join(
        [
            f"module {wrapper} (",
            "    input  wire clk,",
            "    input  wire reset,",
            "    input  wire valid_in,",
            f"    input  wire signed [{input_bits - 1}:0] x_flat,",
            "    output wire valid_out,",
            f"    output wire signed [{output_bits - 1}:0] y_flat",
            ");",
            f"  {routed} routed (",
            "      .clk(clk),",
            "      .reset(reset),",
            "      .valid_in(valid_in),",
            "      .x_flat(x_flat),",
            "      .valid_out(valid_out),",
            "      .y_flat(y_flat)",
            "  );",
            "endmodule",
            "",
        ]
    )


def emit_fault_wrapper(
    *,
    routed_top: str,
    wrapper_top: str,
    input_bits: int,
    output_bits: int,
    fault: str,
) -> str:
    routed = safe_module(routed_top, context="routed top")
    wrapper = safe_module(wrapper_top, context="fault wrapper")
    if fault not in FAULTS:
        raise ValueError(f"unsupported fault: {fault}")
    routed_reset = "1'b0" if fault == "reset" else "reset"
    lines = [
        f"module {wrapper} (",
        "    input  wire clk,",
        "    input  wire reset,",
        "    input  wire valid_in,",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        "    output wire valid_out,",
        f"    output wire signed [{output_bits - 1}:0] y_flat",
        ");",
        "  wire routed_valid;",
        f"  wire signed [{output_bits - 1}:0] routed_y;",
        f"  {routed} routed (",
        "      .clk(clk),",
        f"      .reset({routed_reset}),",
        "      .valid_in(valid_in),",
        "      .x_flat(x_flat),",
        "      .valid_out(routed_valid),",
        "      .y_flat(routed_y)",
        "  );",
    ]
    if fault == "data":
        lines.extend(
            [
                f"  wire [{output_bits - 1}:0] data_fault_mask;",
                (
                    f"  assign data_fault_mask = {{{{{output_bits - 1}{{1'b0}}}}, "
                    "(routed_valid & x_flat[0])};"
                ),
                "  assign valid_out = routed_valid;",
                "  assign y_flat = routed_y ^ data_fault_mask;",
            ]
        )
    elif fault == "valid":
        lines.extend(
            [
                "  reg delayed_valid;",
                "  always @(posedge clk) begin",
                "    if (reset)",
                "      delayed_valid <= 1'b0;",
                "    else",
                "      delayed_valid <= routed_valid;",
                "  end",
                "  assign valid_out = delayed_valid;",
                "  assign y_flat = routed_y;",
            ]
        )
    else:
        lines.extend(
            [
                "  assign valid_out = routed_valid;",
                "  assign y_flat = routed_y;",
            ]
        )
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def run_command(
    executable: str,
    workdir: Path,
    script: Path,
    *,
    timeout: int,
) -> dict[str, Any]:
    process = subprocess.Popen(
        [executable, "-s", script.name],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()

    stdout_path = workdir / f"{script.stem}.stdout.txt"
    stderr_path = workdir / f"{script.stem}.stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined = stdout + "\n" + stderr
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "positive_passed": (
            not timed_out and process.returncode == 0 and SUCCESS_MARKER in combined
        ),
        "negative_control_detected": (
            not timed_out
            and process.returncode != 0
            and SUCCESS_MARKER not in combined
            and UNPROVEN_MARKER in combined
        ),
        "success_marker_found": SUCCESS_MARKER in combined,
        "unproven_marker_found": UNPROVEN_MARKER in combined,
        "stdout": stdout_path.name,
        "stdout_sha256": sha256(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": sha256(stderr_path),
    }


def build_probe(
    physical_root: Path,
    models: Path,
    output: Path,
    *,
    yosys: str,
    timeout: int,
) -> dict[str, Any]:
    root = physical_root.resolve()
    model_path = models.resolve()
    if not model_path.is_file() or model_path.is_symlink():
        raise StructuralProbeError(f"functional cell models are missing: {model_path}")
    resolved_yosys = shutil.which(yosys)
    if resolved_yosys is None:
        raise StructuralProbeError(f"Yosys executable was not found: {yosys}")

    physical_path = root / "evidence" / "openroad_physical_evidence.json"
    prepared_path = root / "prepared" / "prepared.json"
    registered_path = root / "prepared" / "registered" / "registered_manifest.json"
    physical = load_json(physical_path)
    prepared = load_json(prepared_path)
    registered = load_json(registered_path)

    if physical.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise StructuralProbeError("unsupported physical evidence schema")
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise StructuralProbeError("unsupported prepared evidence schema")
    if registered.get("schema") != "hephaestus.registered-matched-tiles.v1":
        raise StructuralProbeError("unsupported registered evidence schema")
    if set(physical.get("backends", {})) != set(BACKENDS):
        raise StructuralProbeError("physical backend set differs from the matched contract")
    if set(prepared.get("backends", {})) != set(BACKENDS):
        raise StructuralProbeError("prepared backend set differs from the matched contract")
    registered_contract = registered.get("contract")
    if not isinstance(registered_contract, dict):
        raise StructuralProbeError("registered contract is malformed")
    input_bits = registered_contract.get("input_bits")
    output_bits = registered_contract.get("output_bits")
    if type(input_bits) is not int or input_bits <= 0:
        raise StructuralProbeError("registered input width is invalid")
    if type(output_bits) is not int or output_bits <= 0:
        raise StructuralProbeError("registered output width is invalid")

    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for backend in BACKENDS:
        prepared_backend = prepared["backends"][backend]
        physical_backend = physical["backends"][backend]
        runs = physical_backend.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise StructuralProbeError(f"{backend} does not have two physical runs")

        routed_paths: dict[int, Path] = {}
        routed_digests: dict[int, str] = {}
        for attempt in (1, 2):
            run = next((item for item in runs if item.get("attempt") == attempt), None)
            if not isinstance(run, dict):
                raise StructuralProbeError(f"{backend} attempt {attempt} is missing")
            routed_meta = run.get("artifacts", {}).get("final_verilog")
            if not isinstance(routed_meta, dict):
                raise StructuralProbeError(
                    f"{backend} attempt {attempt} routed-Verilog metadata is missing"
                )
            attempt_root = (
                root
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            )
            routed = exactly_one(attempt_root, "6_final.v")
            expected_routed = routed_meta.get("sha256")
            actual_routed = sha256(routed)
            if actual_routed != expected_routed:
                raise StructuralProbeError(
                    f"{backend} attempt {attempt} routed-Verilog digest mismatch"
                )
            routed_paths[attempt] = routed
            routed_digests[attempt] = actual_routed
        if routed_digests[1] != routed_digests[2]:
            raise StructuralProbeError(
                f"{backend} physical attempts do not share one routed netlist"
            )

        registered_root = root / "prepared" / "registered"
        source_core = registered_root / prepared_backend["core_rtl"]
        source_wrapper = registered_root / prepared_backend["wrapper_rtl"]
        for label, path, expected in (
            ("core", source_core, prepared_backend["core_sha256"]),
            ("wrapper", source_wrapper, prepared_backend["wrapper_sha256"]),
        ):
            if not path.is_file() or path.is_symlink() or sha256(path) != expected:
                raise StructuralProbeError(f"{backend} source {label} binding differs")

        backend_dir = output / backend
        backend_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_core, backend_dir / "source_core.sv")
        shutil.copyfile(source_wrapper, backend_dir / "source_wrapper.sv")
        shutil.copyfile(routed_paths[1], backend_dir / "routed.v")
        shutil.copyfile(model_path, backend_dir / "models.v")

        routed_top = prepared_backend["wrapper_module"]
        positive_top = f"{routed_top}_positive"
        (backend_dir / "fault_wrapper.sv").write_text(
            emit_passthrough_wrapper(
                routed_top=routed_top,
                wrapper_top=positive_top,
                input_bits=input_bits,
                output_bits=output_bits,
            ),
            encoding="utf-8",
        )
        positive_runs: list[dict[str, Any]] = []
        for attempt in (1, 2):
            script = backend_dir / f"positive-{attempt}.ys"
            script.write_text(
                emit_equivalence_script(
                    source_top=routed_top,
                    gate_top=positive_top,
                ),
                encoding="utf-8",
            )
            result = run_command(
                resolved_yosys,
                backend_dir,
                script,
                timeout=timeout,
            )
            positive_runs.append(result)
            if not result["positive_passed"]:
                break

        controls: dict[str, Any] = {}
        if all(result["positive_passed"] for result in positive_runs):
            for fault in FAULTS:
                fault_top = f"{routed_top}_fault_{fault}"
                (backend_dir / "fault_wrapper.sv").write_text(
                    emit_fault_wrapper(
                        routed_top=routed_top,
                        wrapper_top=fault_top,
                        input_bits=input_bits,
                        output_bits=output_bits,
                        fault=fault,
                    ),
                    encoding="utf-8",
                )
                script = backend_dir / f"negative-{fault}.ys"
                script.write_text(
                    emit_equivalence_script(
                        source_top=routed_top,
                        gate_top=fault_top,
                    ),
                    encoding="utf-8",
                )
                controls[fault] = run_command(
                    resolved_yosys,
                    backend_dir,
                    script,
                    timeout=timeout,
                )

        positive_passed = len(positive_runs) == 2 and all(
            result["positive_passed"] for result in positive_runs
        )
        controls_passed = set(controls) == set(FAULTS) and all(
            result["negative_control_detected"] for result in controls.values()
        )
        results[backend] = {
            "source_core_sha256": sha256(source_core),
            "source_wrapper_sha256": sha256(source_wrapper),
            "routed_verilog_sha256": routed_digests[1],
            "both_physical_attempts_share_routed_verilog": True,
            "positive_runs": positive_runs,
            "negative_controls": controls,
            "positive_passed": positive_passed,
            "negative_controls_passed": controls_passed,
            "passed": positive_passed and controls_passed,
        }

    evidence = {
        "schema": "hephaestus.post-physical-structural-probe.v2",
        "research_only": True,
        "source": {
            "physical_evidence_sha256": sha256(physical_path),
            "prepared_manifest_sha256": sha256(prepared_path),
            "registered_manifest_sha256": sha256(registered_path),
            "functional_cell_models_sha256": sha256(model_path),
        },
        "tool": {
            "yosys": resolved_yosys,
        },
        "proof_contract": {
            "method": "equiv_make + equiv_struct + equiv_simple + equiv_induct",
            "induction_sequence_length": 4,
            "positive_attempts_per_backend": 2,
            "negative_controls": list(FAULTS),
        },
        "backends": results,
        "claims": {
            "all_three_exact_registered_sources_proved_against_routed_netlists": all(
                value["positive_passed"] for value in results.values()
            ),
            "all_data_valid_reset_negative_controls_detected": all(
                value["negative_controls_passed"] for value in results.values()
            ),
            "post_physical_equivalence_verified": False,
            "comparative_ppa_claim_enabled": False,
            "four_state_semantics_verified": False,
            "timing_annotated_functional_semantics_verified": False,
            "drc_clean": False,
            "lvs_clean": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    path = output / "structural_probe.json"
    path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not all(value["passed"] for value in results.values()):
        raise StructuralProbeError("one or more structural-equivalence probes failed")
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("physical_root", type=Path)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--yosys", default="yosys")
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
            timeout=args.timeout,
        )
    except (OSError, ValueError, StructuralProbeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        "structural probe completed: "
        f"{all(value['passed'] for value in evidence['backends'].values())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
