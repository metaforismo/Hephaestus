"""Matched OpenSTA combinational timing evidence for mapped Hephaestus backends."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .report import sha256_file, write_json


class TimingEvidenceError(RuntimeError):
    """Raised when timing evidence cannot be produced safely."""


_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TimingEvidenceError(f"cannot read JSON artifact {path}: {exc}") from exc


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise TimingEvidenceError(f"OpenSTA executable was not found: {requested!r}")
    return resolved


def _tool_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not output:
        raise TimingEvidenceError(f"cannot identify OpenSTA version using {executable!r}")
    return output.splitlines()[0]


def _safe_relative(root: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise TimingEvidenceError(f"artifact path must be relative: {raw_path!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise TimingEvidenceError(f"artifact escapes its bundle root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise TimingEvidenceError(f"artifact does not exist: {resolved}")
    return resolved


def _contains_value(value: Any, expected: str) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_value(child, expected) for child in value.values())
    if isinstance(value, list):
        return any(_contains_value(child, expected) for child in value)
    return False


def _required_number(mapping: dict[str, Any], key: str, context: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TimingEvidenceError(f"{context}.{key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise TimingEvidenceError(f"{context}.{key} must be finite")
    return result


def _load_contract(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise TimingEvidenceError("timing contract must be a JSON object")
    if raw.get("schema") != "hephaestus.combinational-timing-contract.v1":
        raise TimingEvidenceError("unsupported timing-contract schema")
    contract_id = raw.get("contract_id")
    technology_id = raw.get("technology_id")
    clock_name = raw.get("virtual_clock_name")
    if not all(isinstance(value, str) and value for value in (contract_id, technology_id, clock_name)):
        raise TimingEvidenceError("timing contract identifiers must be non-empty strings")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(clock_name)) is None:
        raise TimingEvidenceError("virtual clock name is unsafe")
    positive = ("virtual_clock_period_ns", "input_transition_ns", "output_load_pf")
    nonnegative = ("input_delay_ns", "output_delay_ns")
    for key in positive:
        if _required_number(raw, key, "timing_contract") <= 0:
            raise TimingEvidenceError(f"timing_contract.{key} must be positive")
    for key in nonnegative:
        if _required_number(raw, key, "timing_contract") < 0:
            raise TimingEvidenceError(f"timing_contract.{key} must be non-negative")
    group_count = raw.get("group_count")
    digits = raw.get("digits")
    if type(group_count) is not int or group_count <= 0:
        raise TimingEvidenceError("timing_contract.group_count must be a positive integer")
    if type(digits) is not int or not 1 <= digits <= 12:
        raise TimingEvidenceError("timing_contract.digits must be an integer in [1, 12]")
    if raw.get("path_delay") != "max":
        raise TimingEvidenceError("this evidence level supports only maximum-delay paths")
    return raw


def _validate_sources(
    mapped: dict[str, Any],
    mapped_formal: dict[str, Any],
    mapped_manifest_path: Path,
) -> None:
    if mapped.get("schema") != "hephaestus.standard-cell-mapped-evidence.v1":
        raise TimingEvidenceError("unsupported mapped-evidence schema")
    mapped_claims = mapped.get("claims", {})
    if not isinstance(mapped_claims, dict):
        raise TimingEvidenceError("mapped claims are malformed")
    required_mapped = (
        "matched_integer_contract_verified",
        "standard_cell_mapping_performed",
        "mapped_netlist_structurally_checked",
        "post_mapping_library_area_estimated",
    )
    if not all(mapped_claims.get(name) is True for name in required_mapped):
        raise TimingEvidenceError("mapped evidence does not satisfy the timing prerequisite")

    schema = mapped_formal.get("schema")
    if not isinstance(schema, str) or "mapped" not in schema or "formal" not in schema:
        raise TimingEvidenceError("unsupported mapped-formal evidence schema")
    formal_claims = mapped_formal.get("claims", {})
    if not isinstance(formal_claims, dict) or not all(
        formal_claims.get(name) is True
        for name in (
            "mapped_gate_level_equivalence_verified",
            "exhaustive_combinational_equivalence_verified",
            "negative_control_counterexample_found",
        )
    ):
        raise TimingEvidenceError("mapped formal equivalence must pass before timing analysis")
    mapped_digest = sha256_file(mapped_manifest_path)
    if not _contains_value(mapped_formal, mapped_digest):
        raise TimingEvidenceError("mapped-formal evidence does not bind the mapped manifest digest")


def _build_script(module: str, contract: dict[str, Any]) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise TimingEvidenceError(f"unsafe mapped module name: {module!r}")
    clock = contract["virtual_clock_name"]
    commands = [
        "read_liberty ../../technology/technology.lib",
        "read_verilog input.v",
        f"link_design {module}",
        f"create_clock -name {clock} -period {contract['virtual_clock_period_ns']}",
        f"set_input_delay {contract['input_delay_ns']} -clock {clock} [all_inputs]",
        f"set_output_delay {contract['output_delay_ns']} -clock {clock} [all_outputs]",
        f"set_input_transition {contract['input_transition_ns']} [all_inputs]",
        f"set_load {contract['output_load_pf']} [all_outputs]",
        "check_setup",
        'puts "HEPHAESTUS_BEGIN_UNCONSTRAINED"',
        f"report_checks -unconstrained -path_delay max -group_count {contract['group_count']} "
        f"-digits {contract['digits']}",
        'puts "HEPHAESTUS_END_UNCONSTRAINED"',
        'puts "HEPHAESTUS_BEGIN_MAX_PATHS"',
        f"report_checks -path_delay max -group_count {contract['group_count']} "
        f"-digits {contract['digits']}",
        'puts "HEPHAESTUS_END_MAX_PATHS"',
        "report_design_area",
        "report_units",
        "exit",
    ]
    return "\n".join(commands) + "\n"


def _section(text: str, begin: str, end: str) -> str:
    pattern = re.compile(re.escape(begin) + r"\n(.*?)\n" + re.escape(end), re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise TimingEvidenceError(f"OpenSTA output is missing section {begin!r}")
    return match.group(1)


def _parse_output(text: str, module: str) -> dict[str, Any]:
    if re.search(r"(^|\n)Error:", text):
        errors = [line for line in text.splitlines() if line.startswith("Error:")]
        raise TimingEvidenceError(f"OpenSTA reported errors for {module!r}: {errors[:3]}")
    unconstrained = _section(
        text,
        "HEPHAESTUS_BEGIN_UNCONSTRAINED",
        "HEPHAESTUS_END_UNCONSTRAINED",
    )
    maximum = _section(text, "HEPHAESTUS_BEGIN_MAX_PATHS", "HEPHAESTUS_END_MAX_PATHS")
    if "Startpoint:" in unconstrained or "Endpoint:" in unconstrained:
        raise TimingEvidenceError(f"OpenSTA found unconstrained paths for {module!r}")
    arrivals = [
        float(value)
        for value in re.findall(
            r"^\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s+data arrival time\s*$",
            maximum,
            re.MULTILINE,
        )
    ]
    slacks = [
        float(value)
        for value in re.findall(
            r"^\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s+slack \((?:MET|VIOLATED)\)\s*$",
            maximum,
            re.MULTILINE,
        )
    ]
    if not arrivals:
        raise TimingEvidenceError(f"OpenSTA reported no constrained arrival time for {module!r}")
    startpoints = re.findall(r"^Startpoint:\s*(.+)$", maximum, re.MULTILINE)
    endpoints = re.findall(r"^Endpoint:\s*(.+)$", maximum, re.MULTILINE)
    return {
        "reported_path_count": len(arrivals),
        "arrival_times_ns": arrivals,
        "worst_data_arrival_ns": max(arrivals),
        "best_data_arrival_ns": min(arrivals),
        "slacks_ns": slacks,
        "minimum_reported_slack_ns": min(slacks) if slacks else None,
        "startpoints": startpoints,
        "endpoints": endpoints,
        "unconstrained_paths_found": False,
        "warning_lines": [line for line in text.splitlines() if line.startswith("Warning:")],
    }


def _run_sta(
    *,
    source_netlist: Path,
    module: str,
    contract: dict[str, Any],
    run_dir: Path,
    executable: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_netlist, run_dir / "input.v")
    (run_dir / "run.tcl").write_text(_build_script(module, contract), encoding="utf-8")
    completed = subprocess.run(
        [executable, "-exit", "run.tcl"],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    stdout = run_dir / "opensta.stdout.txt"
    stderr = run_dir / "opensta.stderr.txt"
    stdout.write_text(completed.stdout, encoding="utf-8")
    stderr.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise TimingEvidenceError(
            f"OpenSTA failed for backend {run_dir.name!r}; inspect preserved logs"
        )
    metrics = _parse_output(completed.stdout + "\n" + completed.stderr, module)
    artifacts = {
        "input_netlist": run_dir / "input.v",
        "script": run_dir / "run.tcl",
        "stdout": stdout,
        "stderr": stderr,
    }
    return {"metrics": metrics, "artifacts": artifacts}


def _repeat(
    *,
    source_netlist: Path,
    module: str,
    contract: dict[str, Any],
    output: Path,
    backend: str,
    executable: str,
    timeout_seconds: int,
    first: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Path]]:
    second = _run_sta(
        source_netlist=source_netlist,
        module=module,
        contract=contract,
        run_dir=output / "repeatability" / backend,
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    compared = ("script", "stdout", "stderr")
    identical = {
        label: sha256_file(first["artifacts"][label])
        == sha256_file(second["artifacts"][label])
        for label in compared
    }
    metrics_equal = first["metrics"] == second["metrics"]
    if not all(identical.values()) or not metrics_equal:
        raise TimingEvidenceError(
            f"OpenSTA timing is not repeatable for backend {backend!r}: "
            f"artifacts={identical}, metrics={metrics_equal}"
        )
    return (
        {
            "performed": True,
            "passed": True,
            "byte_identical_artifacts": identical,
            "normalized_metrics_identical": metrics_equal,
        },
        second["artifacts"],
    )


def _comparisons(backends: dict[str, Any]) -> dict[str, Any]:
    shared = backends.get("shared_dag")
    if not isinstance(shared, dict):
        return {}
    shared_delay = float(shared["metrics"]["worst_data_arrival_ns"])
    result: dict[str, Any] = {}
    for name, backend in sorted(backends.items()):
        if name == "shared_dag":
            continue
        delay = float(backend["metrics"]["worst_data_arrival_ns"])
        result[name] = {
            "shared_dag_delay_difference_ns": delay - shared_delay,
            "shared_dag_delay_ratio": shared_delay / delay,
            "shared_dag_delay_reduction_percent": 100.0 * (delay - shared_delay) / delay,
        }
    return result


def build_timing_evidence(
    mapped_bundle: Path,
    mapped_formal_bundle: Path,
    timing_contract: Path,
    output_dir: Path,
    *,
    sta: str = "sta",
    verify_repeatability: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Analyze formally verified mapped backends under one OpenSTA timing contract."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    mapped_root = mapped_bundle.resolve()
    formal_root = mapped_formal_bundle.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    mapped_manifest_path = mapped_root / "mapped_evidence.json"
    formal_manifest_path = formal_root / "mapped_formal_evidence.json"
    if not mapped_manifest_path.is_file() or not formal_manifest_path.is_file():
        raise TimingEvidenceError("mapped and mapped-formal manifests are required")
    mapped = _load_json(mapped_manifest_path)
    mapped_formal = _load_json(formal_manifest_path)
    _validate_sources(mapped, mapped_formal, mapped_manifest_path)
    contract = _load_contract(timing_contract.resolve())

    technology = mapped.get("technology")
    if not isinstance(technology, dict):
        raise TimingEvidenceError("mapped technology metadata is malformed")
    technology_id = technology.get("technology_id")
    if contract["technology_id"] != technology_id:
        raise TimingEvidenceError("timing contract targets a different technology")
    liberty_entry = technology.get("liberty_artifact")
    config_entry = technology.get("configuration_artifact")
    if not isinstance(liberty_entry, dict) or not isinstance(config_entry, dict):
        raise TimingEvidenceError("mapped technology artifacts are missing")
    source_liberty = _safe_relative(mapped_root, str(liberty_entry.get("path", "")))
    source_config = _safe_relative(mapped_root, str(config_entry.get("path", "")))
    if sha256_file(source_liberty) != liberty_entry.get("sha256"):
        raise TimingEvidenceError("mapped Liberty digest mismatch")
    if sha256_file(source_config) != config_entry.get("sha256"):
        raise TimingEvidenceError("mapped technology-config digest mismatch")

    executable = _resolve_executable(sta)
    version = _tool_version(executable)
    technology_dir = output / "technology"
    technology_dir.mkdir(parents=True, exist_ok=True)
    preserved_liberty = technology_dir / "technology.lib"
    preserved_config = technology_dir / "technology.json"
    preserved_contract = output / "timing_contract.json"
    preserved_mapped = output / "source_mapped_evidence.json"
    preserved_formal = output / "source_mapped_formal_evidence.json"
    shutil.copyfile(source_liberty, preserved_liberty)
    shutil.copyfile(source_config, preserved_config)
    shutil.copyfile(timing_contract.resolve(), preserved_contract)
    shutil.copyfile(mapped_manifest_path, preserved_mapped)
    shutil.copyfile(formal_manifest_path, preserved_formal)

    backend_specs = mapped.get("backends")
    formal_specs = mapped_formal.get("backends")
    if not isinstance(backend_specs, dict) or not isinstance(formal_specs, dict):
        raise TimingEvidenceError("mapped backend evidence is malformed")
    if set(backend_specs) != set(_BACKENDS) or not set(_BACKENDS).issubset(formal_specs):
        raise TimingEvidenceError("timing evidence requires the three matched backends")

    backend_evidence: dict[str, Any] = {}
    for name in _BACKENDS:
        backend = backend_specs[name]
        formal_backend = formal_specs[name]
        if not isinstance(backend, dict) or not isinstance(formal_backend, dict):
            raise TimingEvidenceError(f"backend evidence {name!r} is malformed")
        module = backend.get("module")
        if not isinstance(module, str) or not module:
            raise TimingEvidenceError(f"backend {name!r} has no module")
        artifact = backend.get("artifacts", {}).get("mapped_verilog")
        if not isinstance(artifact, dict):
            raise TimingEvidenceError(f"backend {name!r} has no mapped Verilog")
        netlist = _safe_relative(mapped_root, str(artifact.get("path", "")))
        digest = sha256_file(netlist)
        if digest != artifact.get("sha256"):
            raise TimingEvidenceError(f"backend {name!r} mapped-netlist digest mismatch")
        if not _contains_value(formal_backend, digest):
            raise TimingEvidenceError(
                f"mapped formal evidence does not bind backend {name!r} netlist"
            )

        result = _run_sta(
            source_netlist=netlist,
            module=module,
            contract=contract,
            run_dir=output / "backends" / name,
            executable=executable,
            timeout_seconds=timeout_seconds,
        )
        repeatability = {"performed": False, "passed": False}
        repeated_artifacts: dict[str, Path] = {}
        if verify_repeatability:
            repeatability, repeated_artifacts = _repeat(
                source_netlist=netlist,
                module=module,
                contract=contract,
                output=output,
                backend=name,
                executable=executable,
                timeout_seconds=timeout_seconds,
                first=result,
            )
        backend_evidence[name] = {
            "module": module,
            "mapped_netlist_sha256": digest,
            "metrics": result["metrics"],
            "repeatability": repeatability,
            "artifacts": {
                label: {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                }
                for label, path in result["artifacts"].items()
            },
            "repeatability_artifacts": {
                label: {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                }
                for label, path in repeated_artifacts.items()
            },
        }

    manifest = {
        "schema": "hephaestus.opensta-combinational-timing-evidence.v1",
        "evidence_level": "opensta_standard_cell_combinational_timing_estimate",
        "source": {
            "mapped_evidence": preserved_mapped.name,
            "mapped_evidence_sha256": sha256_file(preserved_mapped),
            "mapped_formal_evidence": preserved_formal.name,
            "mapped_formal_evidence_sha256": sha256_file(preserved_formal),
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
            },
        },
        "tool": {
            "name": "OpenSTA",
            "requested_executable": sta,
            "version": version,
        },
        "timing_contract": contract,
        "backends": backend_evidence,
        "comparisons_to_shared_dag": _comparisons(backend_evidence),
        "claims": {
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": True,
            "mapped_netlist_structurally_checked": True,
            "mapped_gate_level_equivalence_verified": True,
            "combinational_timing_constraints_applied": True,
            "opensta_timing_analysis_completed": True,
            "unconstrained_paths_found": False,
            "timing_repeatability_verified": verify_repeatability,
            "critical_path_delay_estimated": True,
            "sequential_equivalence_verified": False,
            "timing_closed": False,
            "power_estimated": False,
            "placement_performed": False,
            "routing_performed": False,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "timing_evidence.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze mapped, formally verified backends under one OpenSTA contract."
    )
    parser.add_argument("mapped_bundle", type=Path)
    parser.add_argument("--mapped-formal", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/timing-evidence"))
    parser.add_argument("--sta", default="sta")
    parser.add_argument("--verify-repeatability", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_timing_evidence(
            arguments.mapped_bundle,
            arguments.mapped_formal,
            arguments.contract,
            arguments.out,
            sta=arguments.sta,
            verify_repeatability=arguments.verify_repeatability,
            timeout_seconds=arguments.timeout,
        )
    except (TimingEvidenceError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"timed {len(manifest['backends'])} backends at evidence level "
        f"{manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
