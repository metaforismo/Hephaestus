#!/usr/bin/env python3
"""Prepare, run, and normalize the temporary OpenSTA SDC research probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
DEFAULT_LABELS = ("unconstrained", "d4000ps")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class ProbeError(RuntimeError):
    """Raised when the research probe cannot produce trustworthy evidence."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read JSON file {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_artifact(root: Path, entry: Any, *, context: str) -> Path:
    if not isinstance(entry, dict):
        raise ProbeError(f"{context} must be an artifact object")
    raw_path = entry.get("path")
    expected_digest = entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise ProbeError(f"{context}.path must be a non-empty string")
    if not isinstance(expected_digest, str) or _SHA256_RE.fullmatch(expected_digest) is None:
        raise ProbeError(f"{context}.sha256 must be a lowercase SHA-256 digest")

    relative = Path(raw_path)
    if relative.is_absolute():
        raise ProbeError(f"{context}.path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ProbeError(f"{context}.path escapes the evidence bundle") from exc
    if not resolved.is_file():
        raise ProbeError(f"{context} does not exist: {resolved}")
    actual_digest = _sha256(resolved)
    if actual_digest != expected_digest:
        raise ProbeError(
            f"{context} digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
    return resolved


def _safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise ProbeError(f"{context} is not a safe Verilog module name: {value!r}")
    return value


def _positive_number(value: Any, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbeError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ProbeError(f"{context} must be finite and positive")
    return number


def _tcl_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("Tcl numeric values must be finite")
    return format(value, ".12g")


def _analysis_script(
    *,
    module: str,
    period_ns: float,
    input_delay_ns: float,
    output_delay_ns: float,
    driving_cell: str,
    output_load_pf: float,
) -> str:
    period = _tcl_number(period_ns)
    input_delay = _tcl_number(input_delay_ns)
    output_delay = _tcl_number(output_delay_ns)
    output_load = _tcl_number(output_load_pf)
    return "\n".join(
        [
            "read_liberty ../../technology.lib",
            "read_verilog dut.v",
            f"link_design {module}",
            f"create_clock -name virtual_clock -period {period}",
            (
                "set_input_delay -clock virtual_clock "
                f"{input_delay} [all_inputs]"
            ),
            (
                "set_output_delay -clock virtual_clock "
                f"{output_delay} [all_outputs]"
            ),
            f"set_driving_cell -lib_cell {driving_cell} [all_inputs]",
            f"set_load {output_load} [all_outputs]",
            f'puts "HEPHAESTUS_PERIOD_NS {period}"',
            f'puts "HEPHAESTUS_INPUT_DELAY_NS {input_delay}"',
            f'puts "HEPHAESTUS_OUTPUT_DELAY_NS {output_delay}"',
            f'puts "HEPHAESTUS_OUTPUT_LOAD_PF {output_load}"',
            f'puts "HEPHAESTUS_DRIVING_CELL {driving_cell}"',
            "if { ![check_setup -verbose] } {",
            '  puts "HEPHAESTUS_ERROR check_setup_failed"',
            "  exit 2",
            "}",
            "report_units",
            (
                "report_checks -path_delay max -group_path_count 1 "
                "-endpoint_path_count 1 -sort_by_slack -digits 9 "
                "-fields {slew capacitance input_pin net fanout}"
            ),
            "report_worst_slack -max -digits 9",
            "report_tns -max -digits 9",
            "exit 0",
            "",
        ]
    )


def prepare_probe(
    area_delay_bundle: Path,
    output_dir: Path,
    *,
    period_ns: float,
    input_delay_ns: float,
    output_delay_ns: float,
    driving_cell: str,
    output_load_pf: float,
    labels: tuple[str, ...],
) -> dict[str, Any]:
    """Prepare one SDC analysis directory per selected mapped netlist."""

    if period_ns <= 0:
        raise ValueError("period_ns must be positive")
    if input_delay_ns < 0 or output_delay_ns < 0:
        raise ValueError("input and output delays must be non-negative")
    if output_load_pf <= 0:
        raise ValueError("output_load_pf must be positive")
    if not driving_cell or re.fullmatch(r"[A-Za-z0-9_]+", driving_cell) is None:
        raise ValueError("driving_cell must be a safe Liberty cell name")
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("labels must be non-empty and unique")

    bundle = area_delay_bundle.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle / "abc_area_delay_evidence.json"
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != "hephaestus.abc-area-delay-evidence.v1":
        raise ProbeError("unsupported ABC area-delay evidence schema")
    if manifest.get("evidence_level") != "abc_liberty_area_delay_estimate":
        raise ProbeError("unsupported ABC area-delay evidence level")

    claims = manifest.get("claims")
    required_claims = (
        "matched_integer_contract_verified",
        "technology_aware_abc_mapping_performed",
        "declared_input_driver_model_used",
        "declared_output_load_used",
        "abc_internal_timing_estimated",
        "mapped_netlist_structurally_checked",
        "post_mapping_library_area_estimated",
    )
    if not isinstance(claims, dict) or any(claims.get(name) is not True for name in required_claims):
        raise ProbeError("ABC area-delay source evidence is not fully verified")

    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise ProbeError("ABC area-delay contract is malformed")
    if contract.get("combinational") is not True or contract.get("latency_cycles") != 0:
        raise ProbeError("OpenSTA probe currently requires a zero-cycle combinational contract")

    technology = manifest.get("technology")
    if not isinstance(technology, dict):
        raise ProbeError("ABC area-delay technology metadata is malformed")
    liberty = _resolve_artifact(
        bundle,
        technology.get("liberty_artifact"),
        context="technology.liberty_artifact",
    )
    preserved_liberty = output / "technology.lib"
    shutil.copyfile(liberty, preserved_liberty)
    shutil.copyfile(manifest_path, output / "source_abc_area_delay_evidence.json")

    backends = manifest.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(BACKENDS):
        raise ProbeError("ABC area-delay backend set is not the expected matched set")

    analyses_root = output / "analyses"
    analyses_root.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    mapped_digests: set[str] = set()

    for backend_name in BACKENDS:
        backend = backends[backend_name]
        if not isinstance(backend, dict):
            raise ProbeError(f"backend {backend_name!r} is malformed")
        module = _safe_module(backend.get("module"), context=f"{backend_name}.module")
        runs = backend.get("runs")
        if not isinstance(runs, dict):
            raise ProbeError(f"backend {backend_name!r} has no runs")

        for label in labels:
            run = runs.get(label)
            if not isinstance(run, dict):
                raise ProbeError(f"backend {backend_name!r} has no run {label!r}")
            if run.get("area_cross_check_passed") is not True:
                raise ProbeError(f"run {backend_name}/{label} failed its area cross-check")
            repeatability = run.get("repeatability")
            if (
                not isinstance(repeatability, dict)
                or repeatability.get("performed") is not True
                or repeatability.get("passed") is not True
            ):
                raise ProbeError(f"run {backend_name}/{label} is not repeatable")
            source = _resolve_artifact(
                bundle,
                run.get("artifacts", {}).get("mapped_verilog")
                if isinstance(run.get("artifacts"), dict)
                else None,
                context=f"backends.{backend_name}.runs.{label}.mapped_verilog",
            )
            digest = _sha256(source)
            if digest in mapped_digests:
                raise ProbeError(
                    f"selected run {backend_name}/{label} duplicates another mapped netlist"
                )
            mapped_digests.add(digest)

            run_dir = analyses_root / f"{backend_name}__{label}"
            run_dir.mkdir(parents=True, exist_ok=True)
            dut = run_dir / "dut.v"
            shutil.copyfile(source, dut)
            script = run_dir / "analysis.tcl"
            script.write_text(
                _analysis_script(
                    module=module,
                    period_ns=period_ns,
                    input_delay_ns=input_delay_ns,
                    output_delay_ns=output_delay_ns,
                    driving_cell=driving_cell,
                    output_load_pf=output_load_pf,
                ),
                encoding="utf-8",
            )
            metadata = {
                "backend": backend_name,
                "label": label,
                "module": module,
                "mapped_verilog_sha256": digest,
                "abc_library_area": _positive_number(
                    run.get("library_area"),
                    context=f"{backend_name}/{label}.library_area",
                ),
                "abc_delay_picoseconds": _positive_number(
                    run.get("critical_path_delay_picoseconds"),
                    context=f"{backend_name}/{label}.critical_path_delay_picoseconds",
                ),
                "virtual_clock_period_ns": period_ns,
                "input_delay_ns": input_delay_ns,
                "output_delay_ns": output_delay_ns,
                "driving_cell": driving_cell,
                "output_load_pf": output_load_pf,
                "analysis_script_sha256": _sha256(script),
            }
            _write_json(run_dir / "metadata.json", metadata)
            index.append(
                {
                    "backend": backend_name,
                    "label": label,
                    "directory": run_dir.relative_to(output).as_posix(),
                }
            )

    expected_count = len(BACKENDS) * len(labels)
    if len(index) != expected_count or len(mapped_digests) != expected_count:
        raise ProbeError("selected OpenSTA analyses are not one-to-one with mapped netlists")

    prepared = {
        "schema": "hephaestus.opensta-sdc-prepared.v1",
        "source": {
            "abc_area_delay_evidence_sha256": _sha256(manifest_path),
            "liberty_sha256": _sha256(preserved_liberty),
        },
        "contract": contract,
        "assumptions": {
            "virtual_clock_period_ns": period_ns,
            "input_delay_ns": input_delay_ns,
            "output_delay_ns": output_delay_ns,
            "driving_cell": driving_cell,
            "output_load_pf": output_load_pf,
            "parasitics": None,
            "wire_model": "Liberty/default pre-layout model as interpreted by OpenSTA",
        },
        "analyses": index,
    }
    _write_json(output / "prepared.json", prepared)
    return prepared


def _parse_single(pattern: str, text: str, *, context: str) -> float:
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise ProbeError(f"{context}: expected one timing value, found {matches}")
    value = float(matches[0])
    if not math.isfinite(value):
        raise ProbeError(f"{context}: timing value is not finite")
    return value


def _run_once(
    sta: Path,
    run_dir: Path,
    *,
    attempt: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    stdout_path = run_dir / f"opensta.{attempt}.stdout.txt"
    stderr_path = run_dir / f"opensta.{attempt}.stderr.txt"
    try:
        completed = subprocess.run(
            [str(sta), "analysis.tcl"],
            cwd=run_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"OpenSTA timed out in {run_dir.name}") from exc
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    combined = completed.stdout + "\n" + completed.stderr
    if completed.returncode != 0:
        raise ProbeError(
            f"OpenSTA failed for {run_dir.name}; inspect {stdout_path.name} and {stderr_path.name}"
        )
    if "HEPHAESTUS_ERROR" in combined:
        raise ProbeError(f"OpenSTA setup failed for {run_dir.name}")
    if "Startpoint:" not in combined or "Endpoint:" not in combined:
        raise ProbeError(f"OpenSTA did not report a detailed path for {run_dir.name}")

    period_ns = _parse_single(
        r"^HEPHAESTUS_PERIOD_NS\s+([-+0-9.eE]+)\s*$",
        combined,
        context=f"{run_dir.name}.period",
    )
    worst_slack_ns = _parse_single(
        r"^worst slack max\s+([-+0-9.eE]+)\s*$",
        combined,
        context=f"{run_dir.name}.worst_slack",
    )
    tns_ns = _parse_single(
        r"^tns max\s+([-+0-9.eE]+)\s*$",
        combined,
        context=f"{run_dir.name}.tns",
    )
    data_delay_ns = period_ns - worst_slack_ns
    if data_delay_ns <= 0:
        raise ProbeError(f"OpenSTA reported a non-positive data delay for {run_dir.name}")

    return {
        "returncode": completed.returncode,
        "period_ns": period_ns,
        "worst_slack_ns": worst_slack_ns,
        "total_negative_slack_ns": tns_ns,
        "derived_data_delay_ns": data_delay_ns,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "stdout": stdout_path.name,
        "stderr": stderr_path.name,
    }


def run_probe(
    prepared_dir: Path,
    sta_path: Path,
    tool_metadata_path: Path,
    *,
    attempts: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run OpenSTA repeatedly and normalize its SDC timing evidence."""

    if attempts < 2:
        raise ValueError("attempts must be at least two")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    root = prepared_dir.resolve()
    sta = sta_path.resolve()
    if not sta.is_file() or not sta.stat().st_mode & 0o111:
        raise ProbeError(f"OpenSTA binary is missing or not executable: {sta}")
    tool_metadata = _load_json(tool_metadata_path.resolve())
    if not isinstance(tool_metadata, dict):
        raise ProbeError("tool metadata must be a JSON object")
    expected_binary_digest = tool_metadata.get("binary_sha256")
    if expected_binary_digest != _sha256(sta):
        raise ProbeError("OpenSTA binary digest differs from tool metadata")

    prepared = _load_json(root / "prepared.json")
    if not isinstance(prepared, dict) or prepared.get("schema") != "hephaestus.opensta-sdc-prepared.v1":
        raise ProbeError("prepared OpenSTA manifest is missing or unsupported")
    analyses = prepared.get("analyses")
    if not isinstance(analyses, list) or not analyses:
        raise ProbeError("prepared OpenSTA manifest contains no analyses")

    normalized: list[dict[str, Any]] = []
    for item in analyses:
        if not isinstance(item, dict) or not isinstance(item.get("directory"), str):
            raise ProbeError("prepared OpenSTA analysis entry is malformed")
        relative = Path(item["directory"])
        if relative.is_absolute():
            raise ProbeError("prepared OpenSTA analysis path must be relative")
        run_dir = (root / relative).resolve()
        try:
            run_dir.relative_to(root)
        except ValueError as exc:
            raise ProbeError("prepared OpenSTA analysis path escapes its root") from exc
        metadata = _load_json(run_dir / "metadata.json")
        if not isinstance(metadata, dict):
            raise ProbeError(f"metadata is malformed for {run_dir.name}")

        attempt_results = [
            _run_once(
                sta,
                run_dir,
                attempt=attempt,
                timeout_seconds=timeout_seconds,
            )
            for attempt in range(1, attempts + 1)
        ]
        first = attempt_results[0]
        if any(result != first for result in attempt_results[1:]):
            raise ProbeError(f"OpenSTA output is not byte-identical for {run_dir.name}")
        normalized.append(
            {
                **metadata,
                "timing": first,
                "attempts": attempts,
                "repeatability_passed": True,
            }
        )

    mapped_digests = {result["mapped_verilog_sha256"] for result in normalized}
    if len(mapped_digests) != len(normalized):
        raise ProbeError("normalized OpenSTA results do not cover distinct mapped netlists")

    summary = {
        "schema": "hephaestus.opensta-sdc-probe.v1",
        "evidence_level": "opensta_sdc_pre_layout_timing_probe",
        "tool": tool_metadata,
        "source": prepared.get("source"),
        "contract": prepared.get("contract"),
        "assumptions": prepared.get("assumptions"),
        "results": normalized,
        "claims": {
            "opensta_binary_built_from_pinned_source": True,
            "sdc_constraints_applied": True,
            "setup_checks_passed": True,
            "detailed_max_path_reported": True,
            "pre_layout_timing_analyzed": True,
            "repeatability_verified": True,
            "signoff_sta_performed": False,
            "timing_closed": False,
            "parasitics_annotated": False,
            "placement_performed": False,
            "routing_performed": False,
            "power_estimated": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    _write_json(root / "opensta_sdc_probe.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("area_delay_bundle", type=Path)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--period-ns", type=float, default=4.0)
    prepare.add_argument("--input-delay-ns", type=float, default=0.0)
    prepare.add_argument("--output-delay-ns", type=float, default=0.0)
    prepare.add_argument("--driving-cell", default="sg13g2_buf_4")
    prepare.add_argument("--output-load-pf", type=float, default=0.01)
    prepare.add_argument("--labels", nargs="+", default=list(DEFAULT_LABELS))

    run = subparsers.add_parser("run")
    run.add_argument("prepared_dir", type=Path)
    run.add_argument("--sta", type=Path, required=True)
    run.add_argument("--tool-metadata", type=Path, required=True)
    run.add_argument("--attempts", type=int, default=2)
    run.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_probe(
                arguments.area_delay_bundle,
                arguments.out,
                period_ns=arguments.period_ns,
                input_delay_ns=arguments.input_delay_ns,
                output_delay_ns=arguments.output_delay_ns,
                driving_cell=arguments.driving_cell,
                output_load_pf=arguments.output_load_pf,
                labels=tuple(arguments.labels),
            )
            print(f"prepared {len(result['analyses'])} OpenSTA analyses")
        else:
            result = run_probe(
                arguments.prepared_dir,
                arguments.sta,
                arguments.tool_metadata,
                attempts=arguments.attempts,
                timeout_seconds=arguments.timeout,
            )
            print(f"verified {len(result['results'])} repeatable OpenSTA timing analyses")
    except (ProbeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
