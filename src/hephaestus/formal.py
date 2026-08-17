"""Exhaustive combinational equivalence evidence using the Yosys SAT engine."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .lower import required_accumulator_width
from .report import sha256_file, write_json


IntArray = NDArray[np.int64]


class FormalError(RuntimeError):
    """Raised when formal-equivalence evidence cannot be produced safely."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalError(f"cannot read JSON artifact {path}: {exc}") from exc


def _load_codes(path: Path) -> IntArray:
    try:
        values = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise FormalError(f"cannot read quantized codes {path}: {exc}") from exc
    codes = np.asarray(values, dtype=np.int64)
    if codes.ndim != 2 or codes.size == 0:
        raise FormalError(f"codes must be a non-empty 2-D matrix, got {codes.shape}")
    return codes


def _validate_module_name(module: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise FormalError(f"unsafe or unsupported SystemVerilog module name: {module!r}")
    return module


def _resolve_bundle_artifact(bundle_dir: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise FormalError(f"bundle artifact path must be relative: {raw_path!r}")

    root = bundle_dir.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FormalError(f"bundle artifact escapes its root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise FormalError(f"bundle artifact does not exist: {resolved}")
    return resolved


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise FormalError(f"Yosys executable was not found: {requested!r}")
    return resolved


def _tool_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-V"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not output:
        raise FormalError(f"cannot identify Yosys version using {executable!r}")
    return output.splitlines()[0]


def _signed_literal(width: int, value: int) -> str:
    magnitude = abs(value)
    literal = f"{width}'sd{magnitude}"
    return literal if value >= 0 else f"-{literal}"


def emit_reference_systemverilog(
    codes: NDArray[np.integer],
    *,
    input_width: int,
    accumulator_width: int,
    module_name: str,
) -> str:
    """Emit a behavioral arithmetic reference derived directly from the code matrix."""

    matrix = np.asarray(codes, dtype=np.int64)
    if matrix.ndim != 2 or matrix.size == 0:
        raise ValueError(f"codes must be a non-empty 2-D matrix, got {matrix.shape}")
    minimum_width = required_accumulator_width(matrix, input_width)
    if accumulator_width < minimum_width:
        raise ValueError(
            f"accumulator_width={accumulator_width} is unsafe; "
            f"at least {minimum_width} bits are required"
        )

    rows, columns = matrix.shape
    input_bits = columns * input_width
    output_bits = rows * accumulator_width
    extension = accumulator_width - input_width
    module = _validate_module_name(module_name)
    lines = [
        "// Independent arithmetic reference generated directly from source_codes.npy.",
        "// The reference intentionally uses procedural accumulation, not the DAG IR.",
        f"module {module} (",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        f"    output wire signed [{output_bits - 1}:0] y_flat",
        ");",
        "",
        f"  localparam integer INPUT_WIDTH = {input_width};",
        f"  localparam integer ACC_WIDTH = {accumulator_width};",
        "",
    ]

    for column_index in range(columns):
        lines.extend(
            [
                f"  wire signed [{input_width - 1}:0] x_{column_index};",
                f"  wire signed [{accumulator_width - 1}:0] sx_{column_index};",
                (
                    f"  assign x_{column_index} = "
                    f"x_flat[{column_index * input_width} +: INPUT_WIDTH];"
                ),
            ]
        )
        if extension == 0:
            lines.append(f"  assign sx_{column_index} = x_{column_index};")
        else:
            lines.append(
                f"  assign sx_{column_index} = "
                f"{{{{{extension}{{x_{column_index}[INPUT_WIDTH-1]}}}}, "
                f"x_{column_index}}};"
            )
    lines.append("")

    for row_index, row in enumerate(matrix):
        term_names: list[str] = []
        for column_index, raw_coefficient in enumerate(row):
            coefficient = int(raw_coefficient)
            if coefficient == 0:
                continue
            term = f"product_o{row_index}_i{column_index}"
            term_names.append(term)
            lines.extend(
                [
                    f"  wire signed [{accumulator_width - 1}:0] {term};",
                    (
                        f"  assign {term} = $signed(sx_{column_index}) * "
                        f"{_signed_literal(accumulator_width, coefficient)};"
                    ),
                ]
            )

        accumulator = f"reference_o{row_index}"
        lines.append(f"  reg signed [{accumulator_width - 1}:0] {accumulator};")
        lines.append("  always @* begin")
        lines.append(f"    {accumulator} = {{ACC_WIDTH{{1'b0}}}};")
        for term in term_names:
            lines.append(f"    {accumulator} = {accumulator} + {term};")
        lines.append("  end")
        lines.append(
            f"  assign y_flat[{row_index * accumulator_width} +: ACC_WIDTH] = "
            f"{accumulator};"
        )
        lines.append("")

    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def emit_miter_systemverilog(
    *,
    dut_module: str,
    reference_module: str,
    input_bits: int,
    output_bits: int,
    module_name: str,
    inject_fault: bool = False,
) -> str:
    """Emit a one-bit mismatch miter, optionally with a synthetic data-dependent fault."""

    if input_bits <= 0 or output_bits <= 0:
        raise ValueError("miter bus widths must be positive")
    dut = _validate_module_name(dut_module)
    reference = _validate_module_name(reference_module)
    module = _validate_module_name(module_name)

    lines = [
        f"module {module} (",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        "    output wire mismatch",
        ");",
        f"  wire signed [{output_bits - 1}:0] y_dut;",
        f"  wire signed [{output_bits - 1}:0] y_reference;",
        f"  {dut} dut (",
        "      .x_flat(x_flat),",
        "      .y_flat(y_dut)",
        "  );",
        f"  {reference} reference (",
        "      .x_flat(x_flat),",
        "      .y_flat(y_reference)",
        "  );",
    ]
    if inject_fault:
        lines.extend(
            [
                f"  wire [{output_bits - 1}:0] fault_mask;",
                f"  wire signed [{output_bits - 1}:0] y_faulted;",
                (
                    f"  assign fault_mask = "
                    f"{{{{{output_bits - 1}{{1'b0}}}}, x_flat[0]}};"
                ),
                "  assign y_faulted = y_dut ^ fault_mask;",
                "  assign mismatch = |(y_faulted ^ y_reference);",
            ]
        )
    else:
        lines.append("  assign mismatch = |(y_dut ^ y_reference);")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _proof_script(*, miter_module: str, expect_counterexample: bool) -> str:
    top = _validate_module_name(miter_module)
    sat_command = [
        "sat",
        "-set-def-inputs",
        "-prove mismatch 0",
        "-show-inputs",
        "-show-outputs",
    ]
    if not expect_counterexample:
        sat_command.insert(1, "-verify")
    commands = [
        "read_verilog -sv dut.sv reference.sv miter.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "opt",
        "check",
        " ".join(sat_command),
    ]
    return "\n".join(commands) + "\n"


def _run_sat(
    *,
    source_rtl: Path,
    reference_rtl: Path,
    miter_text: str,
    miter_module: str,
    run_dir: Path,
    executable: str,
    timeout_seconds: int,
    expect_counterexample: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_rtl, run_dir / "dut.sv")
    shutil.copyfile(reference_rtl, run_dir / "reference.sv")
    (run_dir / "miter.sv").write_text(miter_text, encoding="utf-8")
    script_path = run_dir / "proof.ys"
    script_path.write_text(
        _proof_script(
            miter_module=miter_module,
            expect_counterexample=expect_counterexample,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [executable, "-s", script_path.name],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    stdout_path = run_dir / "yosys.stdout.txt"
    stderr_path = run_dir / "yosys.stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    combined = completed.stdout + "\n" + completed.stderr
    proof_success = "SAT proof finished - no model found: SUCCESS!" in combined
    counterexample_found = "SAT proof finished - model found: FAIL!" in combined
    if expect_counterexample:
        passed = completed.returncode == 0 and counterexample_found and not proof_success
    else:
        passed = completed.returncode == 0 and proof_success and not counterexample_found
    if not passed:
        expectation = "a counterexample" if expect_counterexample else "a proof"
        raise FormalError(
            f"Yosys SAT did not produce {expectation} for {run_dir.name!r}; "
            "inspect the preserved logs"
        )

    artifacts = {
        "dut_rtl": run_dir / "dut.sv",
        "reference_rtl": run_dir / "reference.sv",
        "miter_rtl": run_dir / "miter.sv",
        "script": script_path,
        "stdout": stdout_path,
        "stderr": stderr_path,
    }
    return {
        "performed": True,
        "passed": True,
        "expect_counterexample": expect_counterexample,
        "proof_success": proof_success,
        "counterexample_found": counterexample_found,
        "returncode": completed.returncode,
        "artifacts": artifacts,
    }


def build_formal_evidence(
    matched_bundle: Path,
    codes_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    max_input_bits: int = 64,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Exhaustively prove every matched backend against a code-matrix reference."""

    if max_input_bits <= 0:
        raise ValueError("max_input_bits must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    bundle = matched_bundle.resolve()
    source_codes_path = codes_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    matched_manifest_path = bundle / "matched_manifest.json"
    if not matched_manifest_path.is_file():
        raise FormalError(f"matched bundle is missing {matched_manifest_path.name}")
    matched_manifest = _load_json(matched_manifest_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise FormalError("unsupported matched-baseline manifest schema")
    if not matched_manifest.get("claims", {}).get("matched_integer_contract_verified"):
        raise FormalError("matched integer contract must be verified before formal proof")

    if not source_codes_path.is_file():
        raise FormalError(f"quantized codes do not exist: {source_codes_path}")
    expected_codes_hash = matched_manifest.get("artifact_sha256", {}).get("source_codes")
    if not isinstance(expected_codes_hash, str) or not expected_codes_hash:
        raise FormalError("matched manifest does not contain the source-codes digest")
    actual_codes_hash = sha256_file(source_codes_path)
    if actual_codes_hash != expected_codes_hash:
        raise FormalError("source codes do not match the matched-baseline manifest")

    contract = matched_manifest.get("contract")
    if not isinstance(contract, dict):
        raise FormalError("matched manifest contract is malformed")
    required_contract_fields = (
        "input_count",
        "output_count",
        "input_width",
        "accumulator_width",
    )
    if any(type(contract.get(field)) is not int for field in required_contract_fields):
        raise FormalError("matched manifest contract dimensions must be integers")
    input_count = int(contract["input_count"])
    output_count = int(contract["output_count"])
    input_width = int(contract["input_width"])
    accumulator_width = int(contract["accumulator_width"])
    if min(input_count, output_count, input_width, accumulator_width) <= 0:
        raise FormalError("matched manifest contract dimensions must be positive")

    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    if input_bits > max_input_bits:
        raise FormalError(
            f"formal input width {input_bits} exceeds the configured limit "
            f"of {max_input_bits} bits"
        )

    codes = _load_codes(source_codes_path)
    if codes.shape != (output_count, input_count):
        raise FormalError(
            f"codes shape {codes.shape} does not match the contract "
            f"({output_count}, {input_count})"
        )
    minimum_width = required_accumulator_width(codes, input_width)
    if accumulator_width < minimum_width:
        raise FormalError(
            f"contract accumulator width {accumulator_width} is smaller than "
            f"the required width {minimum_width}"
        )

    preserved_manifest = output / "source_matched_manifest.json"
    preserved_codes = output / "source_codes.npy"
    shutil.copyfile(matched_manifest_path, preserved_manifest)
    shutil.copyfile(source_codes_path, preserved_codes)

    reference_module = "hephaestus_formal_reference"
    reference_path = output / "reference.sv"
    reference_path.write_text(
        emit_reference_systemverilog(
            codes,
            input_width=input_width,
            accumulator_width=accumulator_width,
            module_name=reference_module,
        ),
        encoding="utf-8",
    )

    backend_specs = matched_manifest.get("backends")
    if not isinstance(backend_specs, dict) or not backend_specs:
        raise FormalError("matched manifest does not contain backend specifications")
    executable = _resolve_executable(yosys)
    version = _tool_version(executable)

    expected_hashes = matched_manifest.get("artifact_sha256", {})
    if not isinstance(expected_hashes, dict):
        raise FormalError("matched manifest artifact hashes are malformed")
    hash_labels = {
        "shared_dag": "shared_dag_rtl",
        "naive_shift_add": "naive_shift_add_rtl",
        "constant_multipliers": "constant_multiplier_rtl",
    }

    backend_evidence: dict[str, Any] = {}
    for backend_name in sorted(backend_specs):
        specification = backend_specs[backend_name]
        if not isinstance(specification, dict):
            raise FormalError(f"backend specification {backend_name!r} is malformed")
        module = _validate_module_name(str(specification.get("module", "")))
        rtl_value = specification.get("rtl")
        if not isinstance(rtl_value, str) or not rtl_value:
            raise FormalError(f"backend {backend_name!r} does not identify its RTL")
        source_rtl = _resolve_bundle_artifact(bundle, rtl_value)

        hash_label = hash_labels.get(backend_name)
        if hash_label is not None:
            expected_hash = expected_hashes.get(hash_label)
            if not isinstance(expected_hash, str) or sha256_file(source_rtl) != expected_hash:
                raise FormalError(
                    f"backend {backend_name!r} RTL hash does not match the matched manifest"
                )

        miter_module = f"hephaestus_formal_{backend_name}_miter"
        result = _run_sat(
            source_rtl=source_rtl,
            reference_rtl=reference_path,
            miter_text=emit_miter_systemverilog(
                dut_module=module,
                reference_module=reference_module,
                input_bits=input_bits,
                output_bits=output_bits,
                module_name=miter_module,
            ),
            miter_module=miter_module,
            run_dir=output / backend_name,
            executable=executable,
            timeout_seconds=timeout_seconds,
            expect_counterexample=False,
        )
        backend_evidence[backend_name] = {
            "module": module,
            "source_rtl": rtl_value,
            "exhaustive_over_defined_inputs": True,
            "input_bits": input_bits,
            "proof": {
                key: value for key, value in result.items() if key != "artifacts"
            },
            "artifacts": {
                label: {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                }
                for label, path in result["artifacts"].items()
            },
        }

    negative_backend = "shared_dag" if "shared_dag" in backend_specs else sorted(backend_specs)[0]
    negative_spec = backend_specs[negative_backend]
    negative_module = _validate_module_name(str(negative_spec.get("module", "")))
    negative_rtl = _resolve_bundle_artifact(bundle, str(negative_spec.get("rtl", "")))
    negative_miter_module = "hephaestus_formal_negative_control_miter"
    negative_result = _run_sat(
        source_rtl=negative_rtl,
        reference_rtl=reference_path,
        miter_text=emit_miter_systemverilog(
            dut_module=negative_module,
            reference_module=reference_module,
            input_bits=input_bits,
            output_bits=output_bits,
            module_name=negative_miter_module,
            inject_fault=True,
        ),
        miter_module=negative_miter_module,
        run_dir=output / "negative_control",
        executable=executable,
        timeout_seconds=timeout_seconds,
        expect_counterexample=True,
    )

    manifest = {
        "schema": "hephaestus.formal-equivalence-evidence.v1",
        "evidence_level": "yosys_sat_combinational_equivalence",
        "source": {
            "matched_manifest": preserved_manifest.name,
            "matched_manifest_sha256": sha256_file(preserved_manifest),
            "codes": preserved_codes.name,
            "codes_sha256": sha256_file(preserved_codes),
        },
        "tool": {
            "name": "Yosys SAT",
            "requested_executable": yosys,
            "version": version,
        },
        "scope": {
            "domain": contract.get("domain"),
            "input_bits": input_bits,
            "output_bits": output_bits,
            "max_input_bits": max_input_bits,
            "defined_inputs_only": True,
            "combinational": True,
            "sequential_depth": 0,
            "timeout_seconds_per_run": timeout_seconds,
        },
        "reference": {
            "module": reference_module,
            "rtl": reference_path.name,
            "sha256": sha256_file(reference_path),
            "derived_directly_from_codes": True,
            "uses_compilation_plan": False,
        },
        "backends": backend_evidence,
        "negative_control": {
            "backend": negative_backend,
            "fault": "xor output bit 0 with input bit 0",
            "proof": {
                key: value
                for key, value in negative_result.items()
                if key != "artifacts"
            },
            "artifacts": {
                label: {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                }
                for label, path in negative_result["artifacts"].items()
            },
        },
        "claims": {
            "matched_integer_contract_verified": True,
            "exhaustive_combinational_equivalence_verified": True,
            "negative_control_counterexample_found": True,
            "sequential_equivalence_verified": False,
            "standard_cell_mapping_performed": False,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "formal_evidence.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove matched RTL backends against an exact code-matrix reference."
    )
    parser.add_argument("matched_bundle", type=Path)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/formal-evidence"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--max-input-bits", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_formal_evidence(
            arguments.matched_bundle,
            arguments.codes,
            arguments.out,
            yosys=arguments.yosys,
            max_input_bits=arguments.max_input_bits,
            timeout_seconds=arguments.timeout,
        )
    except (FormalError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"proved {len(manifest['backends'])} backends at evidence level "
        f"{manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
