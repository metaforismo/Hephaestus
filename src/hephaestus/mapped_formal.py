"""Exhaustive equivalence evidence for standard-cell mapped Hephaestus netlists."""

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

from .formal import emit_miter_systemverilog, emit_reference_systemverilog
from .lower import required_accumulator_width
from .report import sha256_file, write_json

IntArray = NDArray[np.int64]

_SUCCESS_MARKER = "SAT proof finished - no model found: SUCCESS!"
_COUNTEREXAMPLE_MARKER = "SAT proof finished - model found: FAIL!"


class MappedFormalError(RuntimeError):
    """Raised when mapped-netlist equivalence evidence cannot be produced safely."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappedFormalError(f"cannot read JSON artifact {path}: {exc}") from exc


def _load_codes(path: Path) -> IntArray:
    try:
        values = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise MappedFormalError(f"cannot read quantized codes {path}: {exc}") from exc
    codes = np.asarray(values, dtype=np.int64)
    if codes.ndim != 2 or codes.size == 0:
        raise MappedFormalError(f"codes must be a non-empty 2-D matrix, got {codes.shape}")
    return codes


def _validate_module_name(module: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise MappedFormalError(f"unsafe or unsupported SystemVerilog module name: {module!r}")
    return module


def _resolve_bundle_artifact(bundle_dir: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise MappedFormalError(f"bundle artifact path must be relative: {raw_path!r}")

    root = bundle_dir.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MappedFormalError(f"bundle artifact escapes its root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise MappedFormalError(f"bundle artifact does not exist: {resolved}")
    return resolved


def _resolve_hashed_artifact(
    bundle_dir: Path,
    entry: Any,
    *,
    context: str,
) -> Path:
    if not isinstance(entry, dict):
        raise MappedFormalError(f"{context} must be an artifact object")
    raw_path = entry.get("path")
    expected_hash = entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise MappedFormalError(f"{context}.path must be a non-empty string")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise MappedFormalError(f"{context}.sha256 must be a lowercase SHA-256 digest")
    path = _resolve_bundle_artifact(bundle_dir, raw_path)
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        raise MappedFormalError(
            f"{context} digest does not match: expected {expected_hash}, got {actual_hash}"
        )
    return path


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise MappedFormalError(f"Yosys executable was not found: {requested!r}")
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
        raise MappedFormalError(f"cannot identify Yosys version using {executable!r}")
    return output.splitlines()[0]


def _positive_int(mapping: dict[str, Any], key: str, context: str) -> int:
    value = mapping.get(key)
    if type(value) is not int or value <= 0:
        raise MappedFormalError(f"{context}.{key} must be a positive integer")
    return int(value)


def _contract_dimensions(contract: Any, *, context: str) -> tuple[int, int, int, int]:
    if not isinstance(contract, dict):
        raise MappedFormalError(f"{context} must be a JSON object")
    return (
        _positive_int(contract, "input_count", context),
        _positive_int(contract, "output_count", context),
        _positive_int(contract, "input_width", context),
        _positive_int(contract, "accumulator_width", context),
    )


def _inspect_liberty_functions(path: Path) -> tuple[dict[str, Any], dict[str, tuple[str, ...]]]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MappedFormalError(f"cannot read Liberty file {path}: {exc}") from exc

    library_match = re.search(r"library\s*\(\s*([^\s)]+)\s*\)", text)
    cell_matches = list(
        re.finditer(
            r"^\s*cell\s*\(\s*([^\s)]+)\s*\)\s*\{",
            text,
            re.MULTILINE,
        )
    )
    if library_match is None or not cell_matches:
        raise MappedFormalError("Liberty file is missing its library or cell declarations")

    functional_cells: dict[str, tuple[str, ...]] = {}
    for index, match in enumerate(cell_matches):
        end = cell_matches[index + 1].start() if index + 1 < len(cell_matches) else len(text)
        body = text[match.end() : end]
        functions = tuple(
            re.findall(
                r'\bfunction\s*:\s*"([^"]+)"\s*;',
                body,
            )
        )
        if functions:
            functional_cells[match.group(1)] = functions

    metadata = {
        "sha256": sha256_file(path),
        "bytes": len(raw),
        "library": library_match.group(1),
        "cell_declarations": len(cell_matches),
        "functional_cell_models": len(functional_cells),
    }
    return metadata, functional_cells


def _proof_script(
    *,
    miter_module: str,
    expect_counterexample: bool,
    liberty_path: str = "../../technology/technology.lib",
) -> str:
    top = _validate_module_name(miter_module)
    if re.fullmatch(r"[A-Za-z0-9_./-]+", liberty_path) is None:
        raise MappedFormalError(f"unsafe Liberty path in proof script: {liberty_path!r}")

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
        f"read_liberty -ignore_miss_func {liberty_path}",
        "read_verilog -sv dut.v reference.sv miter.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "opt",
        "check -assert",
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
    shutil.copyfile(source_rtl, run_dir / "dut.v")
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
    proof_success = _SUCCESS_MARKER in combined
    counterexample_found = _COUNTEREXAMPLE_MARKER in combined
    unsupported_cell_error = any(
        marker in combined
        for marker in (
            "Failed to import cell",
            "Cannot open input file",
            "is a blackbox",
        )
    )
    if expect_counterexample:
        passed = (
            completed.returncode == 0
            and counterexample_found
            and not proof_success
            and not unsupported_cell_error
        )
    else:
        passed = (
            completed.returncode == 0
            and proof_success
            and not counterexample_found
            and not unsupported_cell_error
        )
    if not passed:
        expectation = "a counterexample" if expect_counterexample else "a proof"
        raise MappedFormalError(
            f"Yosys SAT did not produce {expectation} for {run_dir.name!r}; "
            "inspect the preserved logs"
        )

    artifacts = {
        "dut_mapped_verilog": run_dir / "dut.v",
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
        "unsupported_cell_error": unsupported_cell_error,
        "returncode": completed.returncode,
        "artifacts": artifacts,
    }


def _artifact_manifest(output: Path, artifacts: dict[str, Path]) -> dict[str, Any]:
    return {
        label: {
            "path": path.relative_to(output).as_posix(),
            "sha256": sha256_file(path),
        }
        for label, path in artifacts.items()
    }


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# Standard-cell mapped formal-equivalence evidence",
        "",
        f"Evidence level: `{manifest['evidence_level']}`",
        "",
        f"Technology: `{manifest['technology']['technology_id']}`",
        "",
        "| Backend | Mapped cells | Input bits | SAT result |",
        "|---|---:|---:|:---:|",
    ]
    for name, backend in sorted(manifest["backends"].items()):
        proof = backend["proof"]
        lines.append(
            f"| `{name}` | {backend['mapped_cell_count']} | {backend['input_bits']} | "
            f"{'proved' if proof['passed'] else 'failed'} |"
        )
    lines.extend(
        [
            "",
            "Every positive proof is exhaustive over all defined input bits in the declared "
            "combinational integer domain. A data-dependent synthetic fault must produce a "
            "counterexample.",
            "",
            "This evidence does not include timing, SDF, X/Z semantics, placement, routing, "
            "parasitics, power, or silicon behavior.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_mapped_formal_evidence(
    mapped_bundle: Path,
    codes_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    max_input_bits: int = 64,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Prove mapped standard-cell netlists against an independent code-matrix reference."""

    if max_input_bits <= 0:
        raise ValueError("max_input_bits must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    bundle = mapped_bundle.resolve()
    source_codes = codes_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    mapped_evidence_path = bundle / "mapped_evidence.json"
    if not mapped_evidence_path.is_file():
        raise MappedFormalError(f"mapped bundle is missing {mapped_evidence_path.name}")
    mapped_manifest = _load_json(mapped_evidence_path)
    if mapped_manifest.get("schema") != "hephaestus.standard-cell-mapped-evidence.v1":
        raise MappedFormalError("unsupported mapped-evidence manifest schema")
    if mapped_manifest.get("evidence_level") != "standard_cell_mapped_area_estimate":
        raise MappedFormalError("unsupported mapped-evidence level")
    mapped_claims = mapped_manifest.get("claims")
    if not isinstance(mapped_claims, dict):
        raise MappedFormalError("mapped-evidence claims are malformed")
    if not mapped_claims.get("matched_integer_contract_verified"):
        raise MappedFormalError("matched integer contract must be verified before mapped proof")
    if not mapped_claims.get("standard_cell_mapping_performed"):
        raise MappedFormalError("standard-cell mapping must be completed before mapped proof")
    if not mapped_claims.get("mapped_netlist_structurally_checked"):
        raise MappedFormalError("mapped netlists must pass structural checks before mapped proof")

    source = mapped_manifest.get("source")
    if not isinstance(source, dict):
        raise MappedFormalError("mapped-evidence source metadata is malformed")
    matched_manifest_value = source.get("matched_manifest")
    expected_matched_hash = source.get("matched_manifest_sha256")
    if not isinstance(matched_manifest_value, str) or not matched_manifest_value:
        raise MappedFormalError("mapped evidence does not identify its matched manifest")
    matched_manifest_path = _resolve_bundle_artifact(bundle, matched_manifest_value)
    if (
        not isinstance(expected_matched_hash, str)
        or sha256_file(matched_manifest_path) != expected_matched_hash
    ):
        raise MappedFormalError("matched manifest digest does not match mapped evidence")
    matched_manifest = _load_json(matched_manifest_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise MappedFormalError("unsupported matched-baseline manifest schema")
    matched_claims = matched_manifest.get("claims")
    if not isinstance(matched_claims, dict):
        raise MappedFormalError("source matched claims are malformed")
    if not matched_claims.get("matched_integer_contract_verified"):
        raise MappedFormalError("source matched contract is not verified")

    mapped_contract = mapped_manifest.get("contract")
    matched_contract = matched_manifest.get("contract")
    if not isinstance(mapped_contract, dict) or not isinstance(matched_contract, dict):
        raise MappedFormalError("mapped or matched contract is malformed")
    input_count, output_count, input_width, accumulator_width = _contract_dimensions(
        mapped_contract,
        context="mapped contract",
    )
    _contract_dimensions(matched_contract, context="matched contract")
    contract_fields = (
        "domain",
        "input_count",
        "output_count",
        "input_width",
        "accumulator_width",
        "combinational",
        "latency_cycles",
    )
    mismatched_fields = [
        field
        for field in contract_fields
        if mapped_contract.get(field) != matched_contract.get(field)
    ]
    if mismatched_fields:
        raise MappedFormalError(
            f"mapped and matched contracts disagree on fields: {mismatched_fields}"
        )
    if mapped_contract.get("combinational") is not True:
        raise MappedFormalError(
            "mapped formal evidence currently requires a combinational contract"
        )
    if mapped_contract.get("latency_cycles") != 0:
        raise MappedFormalError("mapped formal evidence currently requires zero-cycle latency")

    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    if input_bits > max_input_bits:
        raise MappedFormalError(
            f"mapped formal input width {input_bits} exceeds the configured limit of "
            f"{max_input_bits} bits"
        )

    if not source_codes.is_file():
        raise MappedFormalError(f"quantized codes do not exist: {source_codes}")
    matched_hashes = matched_manifest.get("artifact_sha256")
    if not isinstance(matched_hashes, dict):
        raise MappedFormalError("matched manifest artifact hashes are malformed")
    expected_codes_hash = matched_hashes.get("source_codes")
    if not isinstance(expected_codes_hash, str) or not expected_codes_hash:
        raise MappedFormalError("matched manifest does not contain the source-codes digest")
    if sha256_file(source_codes) != expected_codes_hash:
        raise MappedFormalError("source codes do not match the matched-baseline manifest")
    codes = _load_codes(source_codes)
    if codes.shape != (output_count, input_count):
        raise MappedFormalError(
            f"codes shape {codes.shape} does not match the contract ({output_count}, {input_count})"
        )
    minimum_width = required_accumulator_width(codes, input_width)
    if accumulator_width < minimum_width:
        raise MappedFormalError(
            f"contract accumulator width {accumulator_width} is smaller than "
            f"the required width {minimum_width}"
        )

    technology = mapped_manifest.get("technology")
    if not isinstance(technology, dict):
        raise MappedFormalError("mapped-evidence technology metadata is malformed")
    technology_id = technology.get("technology_id")
    if not isinstance(technology_id, str) or not technology_id:
        raise MappedFormalError("mapped evidence does not identify its technology")
    liberty_source = _resolve_hashed_artifact(
        bundle,
        technology.get("liberty_artifact"),
        context="technology.liberty_artifact",
    )
    config_source = _resolve_hashed_artifact(
        bundle,
        technology.get("configuration_artifact"),
        context="technology.configuration_artifact",
    )
    liberty_metadata, functional_cells = _inspect_liberty_functions(liberty_source)
    library = technology.get("library")
    if not isinstance(library, dict):
        raise MappedFormalError("mapped-evidence library metadata is malformed")
    if liberty_metadata["sha256"] != library.get("sha256"):
        raise MappedFormalError("proof Liberty digest differs from mapped-evidence technology")
    if liberty_metadata["library"] != library.get("name"):
        raise MappedFormalError("proof Liberty name differs from mapped-evidence technology")

    technology_config = _load_json(config_source)
    if not isinstance(technology_config, dict):
        raise MappedFormalError("technology configuration must be a JSON object")
    if technology_config.get("schema") != "hephaestus.technology.v1":
        raise MappedFormalError("unsupported technology configuration schema")
    if technology_config.get("technology_id") != technology_id:
        raise MappedFormalError("technology configuration ID differs from mapped evidence")
    configured_library = technology_config.get("library")
    if not isinstance(configured_library, dict):
        raise MappedFormalError("technology configuration library is malformed")
    if configured_library.get("name") != liberty_metadata["library"]:
        raise MappedFormalError("technology configuration names a different Liberty library")
    if configured_library.get("sha256") != liberty_metadata["sha256"]:
        raise MappedFormalError("technology configuration pins a different Liberty digest")

    preserved_mapped = output / "source_mapped_evidence.json"
    preserved_matched = output / "source_matched_manifest.json"
    preserved_codes = output / "source_codes.npy"
    technology_dir = output / "technology"
    technology_dir.mkdir(parents=True, exist_ok=True)
    preserved_liberty = technology_dir / "technology.lib"
    preserved_config = technology_dir / "technology.json"
    shutil.copyfile(mapped_evidence_path, preserved_mapped)
    shutil.copyfile(matched_manifest_path, preserved_matched)
    shutil.copyfile(source_codes, preserved_codes)
    shutil.copyfile(liberty_source, preserved_liberty)
    shutil.copyfile(config_source, preserved_config)

    reference_module = "hephaestus_mapped_formal_reference"
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

    backends = mapped_manifest.get("backends")
    if not isinstance(backends, dict) or not backends:
        raise MappedFormalError("mapped evidence does not contain backend results")
    matched_backends = matched_manifest.get("backends")
    if not isinstance(matched_backends, dict) or not matched_backends:
        raise MappedFormalError("matched manifest does not contain backend specifications")
    if set(backends) != set(matched_backends):
        raise MappedFormalError("mapped and matched backend sets differ")
    resolved_backends: dict[str, dict[str, Any]] = {}
    for backend_name in sorted(backends):
        backend = backends[backend_name]
        if not isinstance(backend, dict):
            raise MappedFormalError(f"mapped backend {backend_name!r} is malformed")
        module = _validate_module_name(str(backend.get("module", "")))
        matched_backend = matched_backends[backend_name]
        if not isinstance(matched_backend, dict):
            raise MappedFormalError(f"matched backend specification {backend_name!r} is malformed")
        if matched_backend.get("module") != module:
            raise MappedFormalError(
                f"mapped backend {backend_name!r} module differs from the matched manifest"
            )
        metrics = backend.get("metrics")
        if not isinstance(metrics, dict):
            raise MappedFormalError(f"mapped backend {backend_name!r} metrics are malformed")
        if metrics.get("input_bits") != input_bits or metrics.get("output_bits") != output_bits:
            raise MappedFormalError(
                f"mapped backend {backend_name!r} widths differ from the contract"
            )
        histogram = metrics.get("cell_type_histogram")
        if not isinstance(histogram, dict) or not histogram:
            raise MappedFormalError(f"mapped backend {backend_name!r} has no cell-type histogram")
        if any(not isinstance(cell, str) or not cell for cell in histogram):
            raise MappedFormalError(f"mapped backend {backend_name!r} has invalid cell-type names")
        if any(type(count) is not int or count <= 0 for count in histogram.values()):
            raise MappedFormalError(f"mapped backend {backend_name!r} has invalid cell counts")
        mapped_cell_count = metrics.get("cell_count")
        if type(mapped_cell_count) is not int or mapped_cell_count <= 0:
            raise MappedFormalError(
                f"mapped backend {backend_name!r} has an invalid total cell count"
            )
        if mapped_cell_count != sum(histogram.values()):
            raise MappedFormalError(
                f"mapped backend {backend_name!r} cell count differs from its histogram"
            )
        used_cells = sorted(histogram)
        missing_functions = sorted(set(used_cells) - set(functional_cells))
        if missing_functions:
            raise MappedFormalError(
                f"mapped backend {backend_name!r} uses cells without functional Liberty "
                f"models: {missing_functions}"
            )
        artifacts = backend.get("artifacts")
        if not isinstance(artifacts, dict):
            raise MappedFormalError(f"mapped backend {backend_name!r} artifacts are malformed")
        mapped_verilog = _resolve_hashed_artifact(
            bundle,
            artifacts.get("mapped_verilog"),
            context=f"backends.{backend_name}.artifacts.mapped_verilog",
        )
        resolved_backends[backend_name] = {
            "mapped_verilog": mapped_verilog,
            "module": module,
            "metrics": metrics,
            "used_cells": used_cells,
        }

    executable = _resolve_executable(yosys)
    version = _tool_version(executable)
    backend_evidence: dict[str, Any] = {}
    for backend_name in sorted(resolved_backends):
        resolved = resolved_backends[backend_name]
        mapped_verilog = resolved["mapped_verilog"]
        module = resolved["module"]
        metrics = resolved["metrics"]
        used_cells = resolved["used_cells"]
        miter_module = f"hephaestus_mapped_formal_{backend_name}_miter"
        result = _run_sat(
            source_rtl=mapped_verilog,
            reference_rtl=reference_path,
            miter_text=emit_miter_systemverilog(
                dut_module=module,
                reference_module=reference_module,
                input_bits=input_bits,
                output_bits=output_bits,
                module_name=miter_module,
            ),
            miter_module=miter_module,
            run_dir=output / "proofs" / backend_name,
            executable=executable,
            timeout_seconds=timeout_seconds,
            expect_counterexample=False,
        )
        histogram = metrics["cell_type_histogram"]
        backend_evidence[backend_name] = {
            "module": module,
            "mapped_verilog_sha256": sha256_file(mapped_verilog),
            "mapped_cell_count": int(metrics.get("cell_count", sum(histogram.values()))),
            "mapped_cell_types": used_cells,
            "functional_library_models_verified": True,
            "exhaustive_over_defined_inputs": True,
            "input_bits": input_bits,
            "proof": {key: value for key, value in result.items() if key != "artifacts"},
            "artifacts": _artifact_manifest(output, result["artifacts"]),
        }

    negative_backend = (
        "shared_dag" if "shared_dag" in resolved_backends else sorted(resolved_backends)[0]
    )
    negative_rtl = resolved_backends[negative_backend]["mapped_verilog"]
    negative_module = resolved_backends[negative_backend]["module"]
    negative_miter_module = "hephaestus_mapped_formal_negative_control_miter"
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
        run_dir=output / "proofs" / "negative_control",
        executable=executable,
        timeout_seconds=timeout_seconds,
        expect_counterexample=True,
    )

    manifest = {
        "schema": "hephaestus.mapped-formal-equivalence-evidence.v1",
        "evidence_level": "yosys_sat_standard_cell_mapped_equivalence",
        "source": {
            "mapped_evidence": preserved_mapped.name,
            "mapped_evidence_sha256": sha256_file(preserved_mapped),
            "matched_manifest": preserved_matched.name,
            "matched_manifest_sha256": sha256_file(preserved_matched),
            "codes": preserved_codes.name,
            "codes_sha256": sha256_file(preserved_codes),
        },
        "technology": {
            "technology_id": technology_id,
            "configuration": {
                "path": preserved_config.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_config),
            },
            "liberty": {
                "path": preserved_liberty.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_liberty),
                "library": liberty_metadata["library"],
                "cell_declarations": liberty_metadata["cell_declarations"],
                "functional_cell_models": liberty_metadata["functional_cell_models"],
            },
        },
        "tool": {
            "name": "Yosys SAT with Liberty functional models",
            "requested_executable": yosys,
            "version": version,
        },
        "scope": {
            "domain": mapped_contract.get("domain"),
            "input_bits": input_bits,
            "output_bits": output_bits,
            "max_input_bits": max_input_bits,
            "defined_inputs_only": True,
            "combinational": True,
            "sequential_depth": 0,
            "timeout_seconds_per_run": timeout_seconds,
            "four_state_semantics": False,
            "timing_semantics": False,
        },
        "reference": {
            "module": reference_module,
            "rtl": reference_path.name,
            "sha256": sha256_file(reference_path),
            "derived_directly_from_codes": True,
            "uses_compilation_plan": False,
        },
        "flow": {
            "script_template": _proof_script(
                miter_module="TOP",
                expect_counterexample=False,
            ).replace("-top TOP", "-top <miter>"),
            "liberty_mode": "functional models loaded with read_liberty -ignore_miss_func",
            "structural_check": "yosys check -assert",
            "proof_engine": "yosys sat",
        },
        "backends": backend_evidence,
        "negative_control": {
            "backend": negative_backend,
            "fault": "xor output bit 0 with input bit 0",
            "proof": {key: value for key, value in negative_result.items() if key != "artifacts"},
            "artifacts": _artifact_manifest(output, negative_result["artifacts"]),
        },
        "claims": {
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": True,
            "mapped_netlist_structurally_checked": True,
            "liberty_functional_models_verified": True,
            "mapped_gate_level_equivalence_verified": True,
            "exhaustive_combinational_equivalence_verified": True,
            "negative_control_counterexample_found": True,
            "sequential_equivalence_verified": False,
            "timing_constrained": False,
            "timing_analyzed": False,
            "power_estimated": False,
            "placement_performed": False,
            "routing_performed": False,
            "post_mapping_library_area_estimated": True,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "mapped_formal_evidence.json", manifest)
    _write_summary(output / "SUMMARY.md", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Prove standard-cell mapped netlists against an exact code-matrix reference.")
    )
    parser.add_argument("mapped_bundle", type=Path)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/mapped-formal-evidence"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--max-input-bits", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_mapped_formal_evidence(
            arguments.mapped_bundle,
            arguments.codes,
            arguments.out,
            yosys=arguments.yosys,
            max_input_bits=arguments.max_input_bits,
            timeout_seconds=arguments.timeout,
        )
    except (
        MappedFormalError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"proved {len(manifest['backends'])} mapped backends at evidence level "
        f"{manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
