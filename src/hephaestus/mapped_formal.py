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


class MappedFormalError(RuntimeError):
    """Raised when mapped-netlist formal evidence cannot be produced safely."""


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


def _validate_relative_tool_path(path: str) -> str:
    if not path or Path(path).is_absolute():
        raise MappedFormalError(f"proof-tool path must be relative: {path!r}")
    if re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None:
        raise MappedFormalError(f"unsafe path in proof script: {path!r}")
    return path


def _resolve_artifact(root: Path, raw_path: str, *, context: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise MappedFormalError(f"{context} path must be relative: {raw_path!r}")

    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise MappedFormalError(f"{context} escapes its bundle root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise MappedFormalError(f"{context} does not exist: {resolved}")
    return resolved


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


def _mapped_proof_script(
    *,
    miter_module: str,
    liberty_path: str = "../../technology/technology.lib",
    expect_counterexample: bool,
) -> str:
    top = _validate_module_name(miter_module)
    liberty = _validate_relative_tool_path(liberty_path)
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
        f"read_liberty -ignore_miss_func {liberty}",
        "read_verilog -sv dut.v reference.sv miter.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "opt",
        "check -assert",
        " ".join(sat_command),
    ]
    return "\n".join(commands) + "\n"


def _run_mapped_sat(
    *,
    mapped_verilog: Path,
    reference_rtl: Path,
    miter_text: str,
    miter_module: str,
    run_dir: Path,
    executable: str,
    timeout_seconds: int,
    expect_counterexample: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mapped_verilog, run_dir / "dut.v")
    shutil.copyfile(reference_rtl, run_dir / "reference.sv")
    miter_path = run_dir / "miter.sv"
    script_path = run_dir / "proof.ys"
    stdout_path = run_dir / "yosys.stdout.txt"
    stderr_path = run_dir / "yosys.stderr.txt"
    miter_path.write_text(miter_text, encoding="utf-8")
    script_path.write_text(
        _mapped_proof_script(
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
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    combined = completed.stdout + "\n" + completed.stderr
    proof_success = "SAT proof finished - no model found: SUCCESS!" in combined
    counterexample_found = "SAT proof finished - model found: FAIL!" in combined
    unsupported_cell_error = (
        "Failed to import cell" in combined
        or re.search(r"\bblackbox\b", combined, re.IGNORECASE) is not None
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
        expectation = "a counterexample" if expect_counterexample else "an equivalence proof"
        raise MappedFormalError(
            f"Yosys SAT did not produce {expectation} for {run_dir.name!r}; "
            "inspect the preserved logs"
        )

    artifacts = {
        "mapped_dut": run_dir / "dut.v",
        "reference_rtl": run_dir / "reference.sv",
        "miter_rtl": miter_path,
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


def _required_positive_contract(contract: Any) -> tuple[int, int, int, int]:
    if not isinstance(contract, dict):
        raise MappedFormalError("mapped evidence contract must be a JSON object")
    fields = ("input_count", "output_count", "input_width", "accumulator_width")
    if any(type(contract.get(field)) is not int for field in fields):
        raise MappedFormalError("mapped evidence contract dimensions must be integers")
    dimensions = tuple(int(contract[field]) for field in fields)
    if min(dimensions) <= 0:
        raise MappedFormalError("mapped evidence contract dimensions must be positive")
    return dimensions


def _artifact_entry(mapping: Any, key: str, *, context: str) -> tuple[str, str]:
    if not isinstance(mapping, dict):
        raise MappedFormalError(f"{context} artifacts must be a JSON object")
    entry = mapping.get(key)
    if not isinstance(entry, dict):
        raise MappedFormalError(f"{context} is missing artifact {key!r}")
    path = entry.get("path")
    digest = entry.get("sha256")
    if not isinstance(path, str) or not path:
        raise MappedFormalError(f"{context}.{key}.path must be a non-empty string")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise MappedFormalError(f"{context}.{key}.sha256 must be a lowercase SHA-256")
    return path, digest


def _copy_with_digest(source: Path, destination: Path, expected_sha256: str) -> None:
    if sha256_file(source) != expected_sha256:
        raise MappedFormalError(
            f"artifact digest mismatch for {source}: expected {expected_sha256}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _relative_artifacts(
    artifacts: dict[str, Path],
    output: Path,
) -> dict[str, dict[str, str]]:
    return {
        label: {
            "path": path.relative_to(output).as_posix(),
            "sha256": sha256_file(path),
        }
        for label, path in artifacts.items()
    }


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    scope = manifest["scope"]
    lines = [
        "# Mapped standard-cell formal-equivalence evidence",
        "",
        f"Technology: `{manifest['technology']['technology_id']}`",
        "",
        f"Defined input domain: `{scope['input_bits']}` bits ",
        f"(`2^{scope['input_bits']}` possible bit patterns)",
        "",
        "| Backend | Mapped cells | Equivalence | Unsupported cells |",
        "|---|---:|:---:|:---:|",
    ]
    for name, backend in sorted(manifest["backends"].items()):
        lines.append(
            f"| `{name}` | {backend['mapped_cell_count']} | "
            f"{'proved' if backend['proof']['passed'] else 'failed'} | "
            f"{'yes' if backend['proof']['unsupported_cell_error'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "The negative control introduced a data-dependent output fault and produced a SAT ",
            "counterexample as required.",
            "",
            "This proves bounded combinational semantics of the preserved mapped netlists under ",
            "the pinned Liberty Boolean functions. It does not prove timing, power, placement, ",
            "routing, parasitics, analog behavior, or fabricated silicon.",
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

    mapped_root = mapped_bundle.resolve()
    source_codes = codes_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    mapped_manifest_path = mapped_root / "mapped_evidence.json"
    if not mapped_manifest_path.is_file():
        raise MappedFormalError(f"mapped bundle is missing {mapped_manifest_path.name}")
    mapped_manifest = _load_json(mapped_manifest_path)
    if mapped_manifest.get("schema") != "hephaestus.standard-cell-mapped-evidence.v1":
        raise MappedFormalError("unsupported mapped-evidence schema")
    mapped_claims = mapped_manifest.get("claims")
    if not isinstance(mapped_claims, dict):
        raise MappedFormalError("mapped-evidence claims are malformed")
    required_claims = (
        "matched_integer_contract_verified",
        "standard_cell_mapping_performed",
        "mapped_netlist_structurally_checked",
    )
    if any(mapped_claims.get(claim) is not True for claim in required_claims):
        raise MappedFormalError(
            "verified matched semantics, standard-cell mapping, and structural checking are "
            "required before mapped formal proof"
        )

    input_count, output_count, input_width, accumulator_width = _required_positive_contract(
        mapped_manifest.get("contract")
    )
    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    if input_bits > max_input_bits:
        raise MappedFormalError(
            f"mapped formal input width {input_bits} exceeds the configured limit of "
            f"{max_input_bits} bits"
        )

    source = mapped_manifest.get("source")
    if not isinstance(source, dict):
        raise MappedFormalError("mapped-evidence source is malformed")
    matched_relative = source.get("matched_manifest")
    matched_digest = source.get("matched_manifest_sha256")
    if not isinstance(matched_relative, str) or not isinstance(matched_digest, str):
        raise MappedFormalError("mapped evidence does not pin its matched manifest")
    matched_manifest_path = _resolve_artifact(
        mapped_root,
        matched_relative,
        context="matched manifest",
    )
    if sha256_file(matched_manifest_path) != matched_digest:
        raise MappedFormalError("preserved matched manifest digest does not match mapped evidence")
    matched_manifest = _load_json(matched_manifest_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise MappedFormalError("unsupported preserved matched-manifest schema")
    if not matched_manifest.get("claims", {}).get("matched_integer_contract_verified"):
        raise MappedFormalError("preserved matched integer contract is not verified")

    if not source_codes.is_file():
        raise MappedFormalError(f"quantized codes do not exist: {source_codes}")
    expected_codes_digest = matched_manifest.get("artifact_sha256", {}).get("source_codes")
    if not isinstance(expected_codes_digest, str):
        raise MappedFormalError("preserved matched manifest does not pin source codes")
    if sha256_file(source_codes) != expected_codes_digest:
        raise MappedFormalError("source codes do not match the preserved matched manifest")
    codes = _load_codes(source_codes)
    if codes.shape != (output_count, input_count):
        raise MappedFormalError(
            f"codes shape {codes.shape} does not match the mapped contract "
            f"({output_count}, {input_count})"
        )
    minimum_width = required_accumulator_width(codes, input_width)
    if accumulator_width < minimum_width:
        raise MappedFormalError(
            f"mapped contract accumulator width {accumulator_width} is smaller than the "
            f"required width {minimum_width}"
        )

    technology = mapped_manifest.get("technology")
    if not isinstance(technology, dict):
        raise MappedFormalError("mapped evidence technology metadata is malformed")
    technology_id = technology.get("technology_id")
    if not isinstance(technology_id, str) or not technology_id:
        raise MappedFormalError("mapped evidence does not identify its technology")
    liberty_relative, liberty_digest = _artifact_entry(
        technology,
        "liberty_artifact",
        context="technology",
    )
    liberty_source = _resolve_artifact(
        mapped_root,
        liberty_relative,
        context="technology Liberty",
    )
    if sha256_file(liberty_source) != liberty_digest:
        raise MappedFormalError("preserved Liberty digest does not match mapped evidence")

    preserved_mapped = output / "source_mapped_evidence.json"
    preserved_matched = output / "source_matched_manifest.json"
    preserved_codes = output / "source_codes.npy"
    preserved_liberty = output / "technology" / "technology.lib"
    shutil.copyfile(mapped_manifest_path, preserved_mapped)
    shutil.copyfile(matched_manifest_path, preserved_matched)
    shutil.copyfile(source_codes, preserved_codes)
    _copy_with_digest(liberty_source, preserved_liberty, liberty_digest)

    config_entry = technology.get("configuration_artifact")
    preserved_config: Path | None = None
    if isinstance(config_entry, dict):
        config_relative, config_digest = _artifact_entry(
            technology,
            "configuration_artifact",
            context="technology",
        )
        config_source = _resolve_artifact(
            mapped_root,
            config_relative,
            context="technology configuration",
        )
        preserved_config = output / "technology" / "technology.json"
        _copy_with_digest(config_source, preserved_config, config_digest)

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

    backend_specs = mapped_manifest.get("backends")
    if not isinstance(backend_specs, dict) or not backend_specs:
        raise MappedFormalError("mapped evidence contains no backend specifications")
    executable = _resolve_executable(yosys)
    version = _tool_version(executable)

    backend_evidence: dict[str, Any] = {}
    resolved_backends: dict[str, tuple[str, Path]] = {}
    for backend_name in sorted(backend_specs):
        backend = backend_specs[backend_name]
        if not isinstance(backend, dict):
            raise MappedFormalError(f"mapped backend {backend_name!r} is malformed")
        module = _validate_module_name(str(backend.get("module", "")))
        mapped_relative, mapped_digest = _artifact_entry(
            backend.get("artifacts"),
            "mapped_verilog",
            context=f"backend {backend_name}",
        )
        mapped_verilog = _resolve_artifact(
            mapped_root,
            mapped_relative,
            context=f"backend {backend_name} mapped Verilog",
        )
        if sha256_file(mapped_verilog) != mapped_digest:
            raise MappedFormalError(
                f"mapped Verilog digest does not match evidence for {backend_name!r}"
            )
        metrics = backend.get("metrics")
        if not isinstance(metrics, dict):
            raise MappedFormalError(f"mapped metrics for {backend_name!r} are malformed")
        if metrics.get("input_bits") != input_bits or metrics.get("output_bits") != output_bits:
            raise MappedFormalError(
                f"mapped port widths for {backend_name!r} differ from the contract"
            )
        mapped_cell_count = metrics.get("cell_count")
        if type(mapped_cell_count) is not int or mapped_cell_count <= 0:
            raise MappedFormalError(f"mapped backend {backend_name!r} has no cells")

        miter_module = f"hephaestus_mapped_formal_{backend_name}_miter"
        result = _run_mapped_sat(
            mapped_verilog=mapped_verilog,
            reference_rtl=reference_path,
            miter_text=emit_miter_systemverilog(
                dut_module=module,
                reference_module=reference_module,
                input_bits=input_bits,
                output_bits=output_bits,
                module_name=miter_module,
            ),
            miter_module=miter_module,
            run_dir=output / "backends" / backend_name,
            executable=executable,
            timeout_seconds=timeout_seconds,
            expect_counterexample=False,
        )
        resolved_backends[backend_name] = (module, mapped_verilog)
        backend_evidence[backend_name] = {
            "module": module,
            "mapped_source": mapped_relative,
            "mapped_source_sha256": mapped_digest,
            "mapped_cell_count": mapped_cell_count,
            "exhaustive_over_defined_inputs": True,
            "input_bits": input_bits,
            "proof": {key: value for key, value in result.items() if key != "artifacts"},
            "artifacts": _relative_artifacts(result["artifacts"], output),
        }

    negative_backend = (
        "shared_dag" if "shared_dag" in resolved_backends else sorted(resolved_backends)[0]
    )
    negative_module, negative_verilog = resolved_backends[negative_backend]
    negative_miter_module = "hephaestus_mapped_formal_negative_control_miter"
    negative_result = _run_mapped_sat(
        mapped_verilog=negative_verilog,
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

    technology_artifacts: dict[str, dict[str, str]] = {
        "liberty": {
            "path": preserved_liberty.relative_to(output).as_posix(),
            "sha256": sha256_file(preserved_liberty),
        }
    }
    if preserved_config is not None:
        technology_artifacts["configuration"] = {
            "path": preserved_config.relative_to(output).as_posix(),
            "sha256": sha256_file(preserved_config),
        }

    manifest = {
        "schema": "hephaestus.mapped-formal-equivalence-evidence.v1",
        "evidence_level": "yosys_sat_standard_cell_mapped_combinational_equivalence",
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
            "liberty_function_model_loaded": True,
            "artifacts": technology_artifacts,
        },
        "tool": {
            "name": "Yosys SAT",
            "requested_executable": yosys,
            "version": version,
        },
        "scope": {
            "domain": mapped_manifest.get("contract", {}).get("domain"),
            "input_bits": input_bits,
            "output_bits": output_bits,
            "max_input_bits": max_input_bits,
            "defined_inputs_only": True,
            "combinational": True,
            "sequential_depth": 0,
            "timeout_seconds_per_run": timeout_seconds,
            "proof_subject": "preserved post-ABC standard-cell mapped Verilog",
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
            "proof": {key: value for key, value in negative_result.items() if key != "artifacts"},
            "artifacts": _relative_artifacts(negative_result["artifacts"], output),
        },
        "claims": {
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": True,
            "mapped_netlist_structurally_checked": True,
            "mapped_gate_level_equivalence_verified": True,
            "exhaustive_combinational_equivalence_verified": True,
            "negative_control_counterexample_found": True,
            "sequential_equivalence_verified": False,
            "timing_constrained": False,
            "timing_analyzed": False,
            "power_estimated": False,
            "placement_performed": False,
            "routing_performed": False,
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
        description=(
            "Prove standard-cell mapped netlists against an independent code-matrix reference."
        )
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
