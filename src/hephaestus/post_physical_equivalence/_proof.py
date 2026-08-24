"""Yosys compositional proof construction and strict result parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ._common import (
    _BASE_CASE_CYCLES,
    _BASE_CASE_PROVE_SKIP,
    _EQUIV_SEQUENCE_LENGTH,
    _FAULTS,
    _INDUCTION_STEP_RE,
    _NEGATIVE_STATUS_RE,
    _RESET_SEQUENCE,
    _SAT_FAILURE_MARKER,
    _SAT_SUCCESS_MARKER,
    _STATUS_RE,
    _SUCCESS_MARKER,
    _YOSYS_VERSION_RE,
    PostPhysicalEquivalenceError,
    _safe_module,
    _sha256,
)


def emit_passthrough_wrapper(
    *,
    routed_top: str,
    wrapper_top: str,
    input_bits: int,
    output_bits: int,
) -> str:
    """Wrap one routed top under a unique name for the gate-side design."""

    routed = _safe_module(routed_top, context="routed top")
    wrapper = _safe_module(wrapper_top, context="passthrough wrapper")
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
    """Emit one independent fault used to prove that the gate is non-vacuous."""

    routed = _safe_module(routed_top, context="routed top")
    wrapper = _safe_module(wrapper_top, context="fault wrapper")
    if fault not in _FAULTS:
        raise ValueError(f"unsupported fault mode: {fault}")
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
                "  assign valid_out = routed_valid;",
                f"  assign y_flat = routed_y ^ {output_bits}'d1;",
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


def _normalized_design_commands(*, source_top: str, gate_top: str) -> list[str]:
    source = _safe_module(source_top, context="source top")
    gate = _safe_module(gate_top, context="gate top")
    return [
        "# Normalize the exact registered source implementation.",
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
        "# Normalize the exact routed netlist with functional-only cell models.",
        "read_verilog -sv models.v routed.v gate_wrapper.sv",
        f"hierarchy -check -top {gate}",
        "proc",
        "async2sync",
        "flatten",
        "memory",
        "opt -full",
        f"rename {gate} gate",
        "design -stash gate_design",
        "",
        "# Import both normalized implementations into one equivalence design.",
        "design -copy-from gold_design gold",
        "design -copy-from gate_design gate",
        "equiv_make gold gate equiv",
        "hierarchy -check -top equiv",
        "proc",
        "opt -full",
    ]


def emit_bounded_reset_script(
    *,
    source_top: str,
    gate_top: str,
    expect_counterexample: bool,
) -> str:
    """Prove or falsify the reset-synchronized base case for induction."""

    sat = [
        "sat",
        f"-seq {_BASE_CASE_CYCLES}",
        "-set-def-inputs",
        "-set-init-def",
        f"-prove-skip {_BASE_CASE_PROVE_SKIP}",
        "-prove-asserts",
        "-show-inputs",
        "-show-outputs",
    ]
    if not expect_counterexample:
        sat.insert(1, "-verify")
    sat.extend(
        f"-set-at {step} reset {value}" for step, value in enumerate(_RESET_SEQUENCE, start=1)
    )
    return "\n".join(
        [
            *_normalized_design_commands(
                source_top=source_top,
                gate_top=gate_top,
            ),
            "# Preserve every nontrivial output comparison as an assertion miter.",
            "equiv_status",
            "select -clear",
            "select equiv",
            "equiv_miter -assert reset_miter",
            "hierarchy -check -top reset_miter",
            "proc",
            "flatten",
            "opt -full",
            "check",
            "select -clear",
            "select reset_miter",
            " ".join(sat),
            "",
        ]
    )


def emit_equivalence_script(*, source_top: str, gate_top: str) -> str:
    """Prove the steady-state obligation after a separate bounded base case."""

    return "\n".join(
        [
            *_normalized_design_commands(
                source_top=source_top,
                gate_top=gate_top,
            ),
            "# Close the steady-state obligation with temporal induction.",
            "equiv_struct -maxiter 20",
            "equiv_simple",
            f"equiv_induct -seq {_EQUIV_SEQUENCE_LENGTH}",
            "equiv_status -assert",
            "",
        ]
    )


def _run_process(
    executable: str,
    workdir: Path,
    script: Path,
    *,
    timeout: int,
) -> tuple[subprocess.Popen[str], bool, str, str]:
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
    return process, timed_out, stdout, stderr


def _write_logs(
    workdir: Path,
    script: Path,
    stdout: str,
    stderr: str,
) -> tuple[Path, Path]:
    stdout_path = workdir / f"{script.stem}.stdout.txt"
    stderr_path = workdir / f"{script.stem}.stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return stdout_path, stderr_path


def _run_bounded_yosys(
    executable: str,
    workdir: Path,
    script: Path,
    *,
    timeout: int,
    expect_counterexample: bool,
) -> dict[str, Any]:
    process, timed_out, stdout, stderr = _run_process(
        executable,
        workdir,
        script,
        timeout=timeout,
    )
    stdout_path, stderr_path = _write_logs(workdir, script, stdout, stderr)
    combined = stdout + "\n" + stderr
    status_matches = list(_STATUS_RE.finditer(stdout))
    final_status = status_matches[-1] if status_matches else None
    total = int(final_status.group("total")) if final_status else None
    sat_started = "Executing SAT pass." in stdout
    proof_success = _SAT_SUCCESS_MARKER in combined
    counterexample = _SAT_FAILURE_MARKER in combined
    nonvacuous = total is not None and total > 0
    if expect_counterexample:
        passed = (
            not timed_out
            and process.returncode == 0
            and sat_started
            and nonvacuous
            and counterexample
            and not proof_success
        )
    else:
        passed = (
            not timed_out
            and process.returncode == 0
            and sat_started
            and nonvacuous
            and proof_success
            and not counterexample
        )
    return {
        "passed": passed,
        "expected_counterexample": expect_counterexample,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "sat_pass_started": sat_started,
        "equiv_cells_total": total,
        "proof_success": proof_success,
        "counterexample_found": counterexample,
        "cycles": _BASE_CASE_CYCLES,
        "prove_skip": _BASE_CASE_PROVE_SKIP,
        "reset_sequence": list(_RESET_SEQUENCE),
        "stdout": stdout_path.name,
        "stdout_sha256": _sha256(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": _sha256(stderr_path),
    }


def _run_yosys(
    executable: str,
    workdir: Path,
    script: Path,
    *,
    timeout: int,
    expect_equivalent: bool,
) -> dict[str, Any]:
    process, timed_out, stdout, stderr = _run_process(
        executable,
        workdir,
        script,
        timeout=timeout,
    )
    stdout_path, stderr_path = _write_logs(workdir, script, stdout, stderr)
    combined = stdout + "\n" + stderr
    status_matches = list(_STATUS_RE.finditer(stdout))
    final_status = status_matches[-1] if status_matches else None
    induction_steps = [int(match.group("step")) for match in _INDUCTION_STEP_RE.finditer(stdout)]
    negative_match = _NEGATIVE_STATUS_RE.search(stderr)

    total = int(final_status.group("total")) if final_status else None
    proven = int(final_status.group("proven")) if final_status else None
    unproven = int(final_status.group("unproven")) if final_status else None
    positive_passed = (
        not timed_out
        and process.returncode == 0
        and final_status is not None
        and total is not None
        and total > 0
        and proven == total
        and unproven == 0
        and _SUCCESS_MARKER in combined
    )
    negative_unproven = int(negative_match.group("unproven")) if negative_match else None
    negative_detected = (
        not timed_out
        and process.returncode != 0
        and _SUCCESS_MARKER not in combined
        and "Executing EQUIV_STATUS pass." in stdout
        and negative_unproven is not None
        and negative_unproven > 0
    )
    passed = positive_passed if expect_equivalent else negative_detected
    return {
        "passed": passed,
        "expected_equivalent": expect_equivalent,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "equivalence_success_marker_found": _SUCCESS_MARKER in combined,
        "equiv_cells_total": total,
        "equiv_cells_proven": proven,
        "equiv_cells_unproven": unproven,
        "negative_unproven_cells": negative_unproven,
        "induction_step_reached": max(induction_steps, default=None),
        "stdout": stdout_path.name,
        "stdout_sha256": _sha256(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": _sha256(stderr_path),
    }


def _capture_yosys_version(executable: str, output: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [executable, "-V"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or _YOSYS_VERSION_RE.fullmatch(value) is None:
        raise PostPhysicalEquivalenceError(
            f"cannot identify the pinned Yosys executable: {value!r} {completed.stderr!r}"
        )
    path = output / "yosys.version.txt"
    path.write_text(value + "\n", encoding="utf-8")
    return {
        "executable": str(Path(executable).resolve()),
        "version": value,
        "version_file": path.name,
        "version_file_sha256": _sha256(path),
    }
