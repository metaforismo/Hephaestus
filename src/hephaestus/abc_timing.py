"""Technology-aware ABC area-delay evidence for matched Hephaestus RTL backends."""

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
from .synthesis import _netlist_metrics


class AbcTimingError(RuntimeError):
    """Raised when ABC area-delay evidence cannot be produced safely."""


_EXPECTED_BACKEND_HASH_LABELS = {
    "shared_dag": "shared_dag_rtl",
    "naive_shift_add": "naive_shift_add_rtl",
    "constant_multipliers": "constant_multiplier_rtl",
}

_STIME_PATTERN = re.compile(
    r'ABC:\s+WireLoad\s*=\s*"(?P<wire_load>[^"]+)"\s+'
    r"Gates\s*=\s*(?P<gates>[0-9]+).*?"
    r"Cap\s*=\s*(?P<capacitance>[0-9.eE+-]+)\s*(?P<capacitance_unit>[A-Za-z]+).*?"
    r"Area\s*=\s*(?P<area>[0-9.eE+-]+).*?"
    r"Delay\s*=\s*(?P<delay>[0-9.eE+-]+)\s*(?P<delay_unit>[A-Za-z]+)",
    re.IGNORECASE,
)
_STAT_AREA_PATTERN = re.compile(
    r"Chip area for module '\\?[^']+':\s*([0-9.eE+-]+)"
)
_LIBRARY_SUMMARY_PATTERN = re.compile(
    r'ABC:\s+Library\s+"(?P<name>[^"]+)".*?has\s+'
    r"(?P<usable>[0-9]+)\s+cells\s*\((?P<skipped>[0-9]+)\s+skipped:",
    re.IGNORECASE,
)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AbcTimingError(f"cannot read JSON artifact {path}: {exc}") from exc


def _validate_module_name(module: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise AbcTimingError(
            f"unsafe or unsupported SystemVerilog module name: {module!r}"
        )
    return module


def _resolve_bundle_artifact(bundle_dir: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise AbcTimingError(f"bundle artifact path must be relative: {raw_path!r}")
    root = bundle_dir.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AbcTimingError(
            f"bundle artifact escapes its root: {raw_path!r}"
        ) from exc
    if not resolved.is_file():
        raise AbcTimingError(f"bundle artifact does not exist: {resolved}")
    return resolved


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise AbcTimingError(f"Yosys executable was not found: {requested!r}")
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
        raise AbcTimingError(f"cannot identify Yosys version using {executable!r}")
    return output.splitlines()[0]


def _required_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AbcTimingError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _required_number(mapping: dict[str, Any], key: str, context: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AbcTimingError(f"{context}.{key} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise AbcTimingError(f"{context}.{key} must be finite")
    return number


def _load_area_delay_config(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise AbcTimingError("ABC area-delay configuration must be a JSON object")
    if raw.get("schema") != "hephaestus.abc-area-delay-config.v1":
        raise AbcTimingError("unsupported ABC area-delay configuration schema")
    _required_string(raw, "evidence_id", "config")

    technology = raw.get("technology")
    io = raw.get("io")
    flow = raw.get("flow")
    targets = raw.get("targets_picoseconds")
    if not isinstance(technology, dict):
        raise AbcTimingError("config.technology must be a JSON object")
    if not isinstance(io, dict):
        raise AbcTimingError("config.io must be a JSON object")
    if not isinstance(flow, dict):
        raise AbcTimingError("config.flow must be a JSON object")
    if not isinstance(targets, list) or not targets:
        raise AbcTimingError("config.targets_picoseconds must be a non-empty list")

    _required_string(technology, "technology_id", "config.technology")
    digest = _required_string(
        technology, "liberty_sha256", "config.technology"
    )
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise AbcTimingError(
            "config.technology.liberty_sha256 must be a lowercase SHA-256 digest"
        )
    _required_string(technology, "liberty_name", "config.technology")

    _required_string(io, "driving_cell", "config.io")
    load = _required_number(io, "output_load_femtofarads", "config.io")
    if load < 0:
        raise AbcTimingError(
            "config.io.output_load_femtofarads must be non-negative"
        )

    if targets[0] is not None or targets.count(None) != 1:
        raise AbcTimingError(
            "config.targets_picoseconds must start with exactly one null target"
        )
    numeric_targets: list[int] = []
    for target in targets[1:]:
        if type(target) is not int or target <= 0:
            raise AbcTimingError(
                "configured ABC delay targets must be positive integer picoseconds"
            )
        numeric_targets.append(target)
    if numeric_targets != sorted(set(numeric_targets)):
        raise AbcTimingError(
            "configured ABC delay targets must be unique and strictly increasing"
        )

    _required_string(flow, "mapper", "config.flow")
    _required_string(flow, "timing_model", "config.flow")
    if flow.get("physical_design") is not False:
        raise AbcTimingError("config.flow.physical_design must remain false")
    if flow.get("signoff_sta") is not False:
        raise AbcTimingError("config.flow.signoff_sta must remain false")
    return raw


def _load_technology_config(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != "hephaestus.technology.v1":
        raise AbcTimingError("unsupported technology configuration schema")
    library = raw.get("library")
    if not isinstance(library, dict):
        raise AbcTimingError("technology.library must be a JSON object")
    _required_string(raw, "technology_id", "technology")
    _required_string(library, "name", "technology.library")
    digest = _required_string(library, "sha256", "technology.library")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise AbcTimingError(
            "technology.library.sha256 must be a lowercase SHA-256 digest"
        )
    byte_count = library.get("bytes")
    if type(byte_count) is not int or byte_count <= 0:
        raise AbcTimingError("technology.library.bytes must be a positive integer")
    return raw


def _cell_blocks(text: str) -> dict[str, str]:
    matches = list(
        re.finditer(
            r"^\s*cell\s*\(\s*([^\s)]+)\s*\)\s*\{",
            text,
            re.MULTILINE,
        )
    )
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks[match.group(1)] = text[match.end() : end]
    return blocks


def _inspect_liberty(
    path: Path,
) -> tuple[dict[str, Any], dict[str, float], dict[str, dict[str, bool]]]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AbcTimingError(f"cannot read Liberty file {path}: {exc}") from exc

    library_match = re.search(r"library\s*\(\s*([^\s)]+)\s*\)", text)
    voltage_match = re.search(r"nom_voltage\s*:\s*([^;]+);", text)
    temperature_match = re.search(r"nom_temperature\s*:\s*([^;]+);", text)
    if library_match is None:
        raise AbcTimingError("Liberty file is missing its library declaration")

    blocks = _cell_blocks(text)
    if not blocks:
        raise AbcTimingError("Liberty file does not contain cell declarations")

    areas: dict[str, float] = {}
    flags: dict[str, dict[str, bool]] = {}
    for name, body in blocks.items():
        area_match = re.search(
            r"^\s*area\s*:\s*([0-9.eE+-]+)\s*;",
            body,
            re.MULTILINE,
        )
        if area_match is None:
            raise AbcTimingError(f"Liberty cell {name!r} does not declare an area")
        area = float(area_match.group(1))
        if not math.isfinite(area) or area < 0:
            raise AbcTimingError(f"Liberty cell {name!r} has an invalid area")
        areas[name] = area
        flags[name] = {
            "has_function": re.search(r"\bfunction\s*:", body) is not None,
            "dont_use": re.search(
                r"\bdont_use\s*:\s*(?:true|1)\s*;", body, re.IGNORECASE
            )
            is not None,
        }

    try:
        voltage = float(voltage_match.group(1)) if voltage_match else None
        temperature = float(temperature_match.group(1)) if temperature_match else None
    except ValueError as exc:
        raise AbcTimingError("Liberty nominal conditions are not numeric") from exc

    metadata = {
        "sha256": sha256_file(path),
        "bytes": len(raw),
        "library": library_match.group(1),
        "nominal_voltage": voltage,
        "nominal_temperature_c": temperature,
        "cell_declarations": len(blocks),
        "cells_with_area": len(areas),
        "cells_with_function": sum(flag["has_function"] for flag in flags.values()),
        "dont_use_cells": sum(flag["dont_use"] for flag in flags.values()),
        "minimum_cell_area": min(areas.values()),
        "maximum_cell_area": max(areas.values()),
    }
    return metadata, areas, flags


def _verify_inputs(
    evidence_config: dict[str, Any],
    technology_config: dict[str, Any],
    liberty_metadata: dict[str, Any],
    cell_flags: dict[str, dict[str, bool]],
) -> None:
    expected_technology = evidence_config["technology"]
    technology_library = technology_config["library"]
    comparisons = {
        "technology_id": (
            expected_technology["technology_id"],
            technology_config["technology_id"],
        ),
        "configured Liberty name": (
            expected_technology["liberty_name"],
            technology_library["name"],
        ),
        "configured Liberty digest": (
            expected_technology["liberty_sha256"],
            technology_library["sha256"],
        ),
        "inspected Liberty name": (
            expected_technology["liberty_name"],
            liberty_metadata["library"],
        ),
        "inspected Liberty digest": (
            expected_technology["liberty_sha256"],
            liberty_metadata["sha256"],
        ),
        "inspected Liberty bytes": (
            technology_library["bytes"],
            liberty_metadata["bytes"],
        ),
    }
    for label, (expected, actual) in comparisons.items():
        if expected != actual:
            raise AbcTimingError(
                f"{label} mismatch: expected {expected!r}, got {actual!r}"
            )

    driver = evidence_config["io"]["driving_cell"]
    if driver not in cell_flags:
        raise AbcTimingError(
            f"configured ABC driving cell {driver!r} is absent from the Liberty"
        )
    if cell_flags[driver]["dont_use"]:
        raise AbcTimingError(
            f"configured ABC driving cell {driver!r} is marked dont_use"
        )
    if not cell_flags[driver]["has_function"]:
        raise AbcTimingError(
            f"configured ABC driving cell {driver!r} has no Boolean function"
        )


def _target_label(target_picoseconds: int | None) -> str:
    return (
        "unconstrained"
        if target_picoseconds is None
        else f"d{target_picoseconds}ps"
    )


def _constraints_text(driver_cell: str, output_load_femtofarads: float) -> str:
    return (
        f"set_driving_cell {driver_cell}\n"
        f"set_load {output_load_femtofarads:.12g}\n"
    )


def _build_script(
    module: str,
    target_picoseconds: int | None,
    *,
    liberty_path: str = "../../../technology/technology.lib",
    constraints_path: str = "../../../constraints/abc.constr",
) -> str:
    top = _validate_module_name(module)
    safe_path = r"[A-Za-z0-9_./-]+"
    for value, label in (
        (liberty_path, "Liberty"),
        (constraints_path, "constraints"),
    ):
        if re.fullmatch(safe_path, value) is None:
            raise AbcTimingError(f"unsafe {label} path in ABC script: {value!r}")
    target = "" if target_picoseconds is None else f" -D {target_picoseconds}"
    commands = [
        f"read_liberty -lib {liberty_path}",
        "read_verilog -sv input.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "opt",
        "techmap",
        "opt",
        (
            f"abc -liberty {liberty_path} -constr {constraints_path}"
            f"{target}"
        ),
        "clean -purge",
        "check -assert",
        f"tee -o mapped.stat.txt stat -liberty {liberty_path}",
        "write_verilog -noattr mapped.v",
        "write_json mapped.json",
    ]
    return "\n".join(commands) + "\n"


def _parse_stime(output: str) -> dict[str, Any]:
    matches = list(_STIME_PATTERN.finditer(output))
    if len(matches) != 1:
        raise AbcTimingError(
            f"expected exactly one ABC stime record, found {len(matches)}"
        )
    match = matches[0]
    record = {
        "wire_load": match.group("wire_load"),
        "gate_count": int(match.group("gates")),
        "capacitance": float(match.group("capacitance")),
        "capacitance_unit": match.group("capacitance_unit").lower(),
        "library_area": float(match.group("area")),
        "delay": float(match.group("delay")),
        "delay_unit": match.group("delay_unit").lower(),
        "raw_line": match.group(0),
    }
    for key in ("capacitance", "library_area", "delay"):
        if not math.isfinite(record[key]) or record[key] < 0:
            raise AbcTimingError(f"ABC stime produced an invalid {key}")
    if record["gate_count"] <= 0 or record["library_area"] <= 0:
        raise AbcTimingError("ABC stime produced an empty or zero-area mapping")
    if record["delay"] <= 0 or record["delay_unit"] != "ps":
        raise AbcTimingError("ABC stime delay must be a positive picosecond value")
    if record["capacitance_unit"] != "ff":
        raise AbcTimingError("ABC stime capacitance must be reported in femtofarads")
    return record


def _parse_stat_area(stat_text: str, module: str) -> float:
    matches = _STAT_AREA_PATTERN.findall(stat_text)
    if len(matches) != 1:
        raise AbcTimingError(
            f"expected one mapped area for module {module!r}, found {len(matches)}"
        )
    area = float(matches[0])
    if not math.isfinite(area) or area <= 0:
        raise AbcTimingError(f"mapped area for module {module!r} is invalid")
    return area


def _library_summary(output: str) -> dict[str, Any]:
    matches = list(_LIBRARY_SUMMARY_PATTERN.finditer(output))
    if len(matches) != 1:
        raise AbcTimingError(
            f"expected exactly one ABC library summary, found {len(matches)}"
        )
    match = matches[0]
    return {
        "name": match.group("name"),
        "usable_cells": int(match.group("usable")),
        "skipped_cells": int(match.group("skipped")),
    }


def _required_run_artifacts(run_dir: Path) -> dict[str, Path]:
    return {
        "input_rtl": run_dir / "input.sv",
        "script": run_dir / "map.ys",
        "stdout": run_dir / "yosys.stdout.txt",
        "stderr": run_dir / "yosys.stderr.txt",
        "mapped_stat": run_dir / "mapped.stat.txt",
        "mapped_verilog": run_dir / "mapped.v",
        "mapped_netlist": run_dir / "mapped.json",
    }


def _run_mapping(
    *,
    source_rtl: Path,
    module: str,
    target_picoseconds: int | None,
    run_dir: Path,
    executable: str,
    cell_areas: dict[str, float],
    expected_input_bits: int,
    expected_output_bits: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    top = _validate_module_name(module)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_rtl, run_dir / "input.sv")
    (run_dir / "map.ys").write_text(
        _build_script(top, target_picoseconds), encoding="utf-8"
    )

    completed = subprocess.run(
        [executable, "-s", "map.ys"],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    (run_dir / "yosys.stdout.txt").write_text(
        completed.stdout, encoding="utf-8"
    )
    (run_dir / "yosys.stderr.txt").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise AbcTimingError(
            f"Yosys/ABC failed for run {run_dir}; inspect preserved logs"
        )

    artifacts = _required_run_artifacts(run_dir)
    missing = [label for label, path in artifacts.items() if not path.is_file()]
    if missing:
        raise AbcTimingError(
            f"Yosys/ABC did not produce required artifacts for {run_dir}: {missing}"
        )

    netlist = _load_json(artifacts["mapped_netlist"])
    metrics = _netlist_metrics(netlist, top)
    if metrics["input_bits"] != expected_input_bits:
        raise AbcTimingError(
            f"mapped input width differs from the contract for {run_dir}"
        )
    if metrics["output_bits"] != expected_output_bits:
        raise AbcTimingError(
            f"mapped output width differs from the contract for {run_dir}"
        )
    if metrics["cell_count"] <= 0:
        raise AbcTimingError(f"ABC mapped {run_dir} to no cells")

    histogram = metrics["cell_type_histogram"]
    unknown = sorted(set(histogram) - set(cell_areas))
    if unknown:
        raise AbcTimingError(
            f"mapped run {run_dir} uses cells absent from the pinned Liberty: {unknown}"
        )

    combined = completed.stdout + "\n" + completed.stderr
    stime = _parse_stime(combined)
    library = _library_summary(combined)
    stat_text = artifacts["mapped_stat"].read_text(encoding="utf-8")
    stat_area = _parse_stat_area(stat_text, top)
    histogram_area = sum(cell_areas[cell] * count for cell, count in histogram.items())

    if stime["gate_count"] != metrics["cell_count"]:
        raise AbcTimingError(
            f"ABC gate count and mapped netlist cell count differ for {run_dir}"
        )
    if not math.isclose(stime["library_area"], stat_area, abs_tol=0.01):
        raise AbcTimingError(
            f"ABC stime area and Yosys stat area differ for {run_dir}: "
            f"ABC={stime['library_area']}, Yosys={stat_area}"
        )
    if not math.isclose(stat_area, histogram_area, rel_tol=1e-12, abs_tol=1e-6):
        raise AbcTimingError(
            f"mapped area histogram cross-check failed for {run_dir}: "
            f"Yosys={stat_area}, histogram={histogram_area}"
        )

    delay_ps = float(stime["delay"])
    target_met = (
        None
        if target_picoseconds is None
        else delay_ps <= float(target_picoseconds)
    )
    target_margin = (
        None
        if target_picoseconds is None
        else float(target_picoseconds) - delay_ps
    )
    return {
        "target_picoseconds": target_picoseconds,
        "target_met": target_met,
        "target_margin_picoseconds": target_margin,
        "metrics": metrics,
        "abc_stime": stime,
        "abc_library": library,
        "library_area": stat_area,
        "library_area_from_histogram": histogram_area,
        "critical_path_delay_picoseconds": delay_ps,
        "area_delay_product": stat_area * delay_ps,
        "area_cross_check_passed": True,
        "structural_check": "yosys check -assert",
        "artifacts": artifacts,
    }


def _verify_repeatability(
    *,
    source_rtl: Path,
    module: str,
    target_picoseconds: int | None,
    output_dir: Path,
    backend_name: str,
    label: str,
    executable: str,
    cell_areas: dict[str, float],
    expected_input_bits: int,
    expected_output_bits: int,
    timeout_seconds: int,
    first_run: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Path]]:
    second = _run_mapping(
        source_rtl=source_rtl,
        module=module,
        target_picoseconds=target_picoseconds,
        run_dir=output_dir / "repeatability" / backend_name / label,
        executable=executable,
        cell_areas=cell_areas,
        expected_input_bits=expected_input_bits,
        expected_output_bits=expected_output_bits,
        timeout_seconds=timeout_seconds,
    )
    compared = ("mapped_netlist", "mapped_verilog", "mapped_stat")
    byte_identical = {
        artifact: sha256_file(first_run["artifacts"][artifact])
        == sha256_file(second["artifacts"][artifact])
        for artifact in compared
    }
    normalized_match = all(
        (
            first_run["metrics"] == second["metrics"],
            math.isclose(
                first_run["library_area"],
                second["library_area"],
                rel_tol=0,
                abs_tol=1e-12,
            ),
            math.isclose(
                first_run["critical_path_delay_picoseconds"],
                second["critical_path_delay_picoseconds"],
                rel_tol=0,
                abs_tol=1e-12,
            ),
            first_run["target_met"] == second["target_met"],
            first_run["abc_library"] == second["abc_library"],
        )
    )
    passed = all(byte_identical.values()) and normalized_match
    if not passed:
        raise AbcTimingError(
            f"ABC area-delay mapping is not repeatable for {backend_name!r}/{label}: "
            f"artifacts={byte_identical}, normalized={normalized_match}"
        )
    return (
        {
            "performed": True,
            "passed": True,
            "byte_identical_artifacts": byte_identical,
            "normalized_metrics_identical": normalized_match,
        },
        second["artifacts"],
    )


def _unique_points(runs: dict[str, Any]) -> list[tuple[str, float, float]]:
    seen: set[tuple[float, float]] = set()
    points: list[tuple[str, float, float]] = []
    for label, run in runs.items():
        point = (
            round(float(run["library_area"]), 12),
            round(float(run["critical_path_delay_picoseconds"]), 12),
        )
        if point in seen:
            continue
        seen.add(point)
        points.append((label, point[0], point[1]))
    return points


def _pareto_labels(runs: dict[str, Any]) -> list[str]:
    points = _unique_points(runs)
    pareto: list[str] = []
    for label, area, delay in points:
        dominated = any(
            other_area <= area
            and other_delay <= delay
            and (other_area < area or other_delay < delay)
            for other_label, other_area, other_delay in points
            if other_label != label
        )
        if not dominated:
            pareto.append(label)
    return pareto


def _minimum_label(runs: dict[str, Any], metric: str) -> str:
    return min(
        runs,
        key=lambda label: (
            float(runs[label][metric]),
            float(
                runs[label][
                    "critical_path_delay_picoseconds"
                    if metric == "library_area"
                    else "library_area"
                ]
            ),
        ),
    )


def _comparisons_to_shared(backends: dict[str, Any]) -> dict[str, Any]:
    if "shared_dag" not in backends:
        return {}
    shared_runs = backends["shared_dag"]["runs"]
    comparisons: dict[str, Any] = {}
    for label, shared in shared_runs.items():
        shared_area = float(shared["library_area"])
        shared_delay = float(shared["critical_path_delay_picoseconds"])
        shared_adp = float(shared["area_delay_product"])
        peers: dict[str, Any] = {}
        for backend_name, backend in sorted(backends.items()):
            if backend_name == "shared_dag":
                continue
            peer = backend["runs"][label]
            area = float(peer["library_area"])
            delay = float(peer["critical_path_delay_picoseconds"])
            adp = float(peer["area_delay_product"])
            peers[backend_name] = {
                "shared_dag_area_difference": area - shared_area,
                "shared_dag_area_reduction_percent": 100.0
                * (area - shared_area)
                / area,
                "shared_dag_delay_difference_picoseconds": delay - shared_delay,
                "shared_dag_delay_reduction_percent": 100.0
                * (delay - shared_delay)
                / delay,
                "shared_dag_area_delay_product_reduction_percent": 100.0
                * (adp - shared_adp)
                / adp,
            }
        comparisons[label] = peers
    return comparisons


def _artifact_entries(
    artifacts: dict[str, Path], output_dir: Path
) -> dict[str, dict[str, str]]:
    return {
        label: {
            "path": path.relative_to(output_dir).as_posix(),
            "sha256": sha256_file(path),
        }
        for label, path in artifacts.items()
    }


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    io = manifest["configuration"]["io"]
    lines = [
        "# IHP SG13G2 ABC area-delay evidence",
        "",
        f"Input driver: `{io['driving_cell']}`",
        "",
        f"Output load: `{io['output_load_femtofarads']}` fF per primary output",
        "",
        "| Backend | Point | Target | Cells | Liberty area | ABC delay (ps) | Target met |",
        "|---|---|---:|---:|---:|---:|:---:|",
    ]
    for backend_name, backend in sorted(manifest["backends"].items()):
        for label, run in backend["runs"].items():
            target = (
                "none"
                if run["target_picoseconds"] is None
                else str(run["target_picoseconds"])
            )
            met = (
                "n/a"
                if run["target_met"] is None
                else ("yes" if run["target_met"] else "no")
            )
            lines.append(
                f"| `{backend_name}` | `{label}` | {target} | "
                f"{run['metrics']['cell_count']} | {run['library_area']:.4f} | "
                f"{run['critical_path_delay_picoseconds']:.2f} | {met} |"
            )
    lines.extend(
        [
            "",
            "The delay is ABC `stime -p` output under the declared Liberty, input-driver, "
            "and output-load assumptions. It is not sign-off STA, an SDC-closed clock, "
            "placed/routed timing, or extracted silicon behavior.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_abc_area_delay_evidence(
    matched_bundle: Path,
    technology_config: Path,
    liberty_path: Path,
    evidence_config: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    verify_repeatability: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Sweep ABC delay targets for every verified matched backend."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    bundle = matched_bundle.resolve()
    technology_path = technology_config.resolve()
    liberty = liberty_path.resolve()
    configuration_path = evidence_config.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    matched_manifest_path = bundle / "matched_manifest.json"
    if not matched_manifest_path.is_file():
        raise AbcTimingError(
            f"matched bundle is missing {matched_manifest_path.name}"
        )
    matched_manifest = _load_json(matched_manifest_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise AbcTimingError("unsupported matched-baseline manifest schema")
    if not matched_manifest.get("claims", {}).get(
        "matched_integer_contract_verified"
    ):
        raise AbcTimingError(
            "matched integer contract must be verified before ABC timing evidence"
        )

    configuration = _load_area_delay_config(configuration_path)
    technology = _load_technology_config(technology_path)
    if not liberty.is_file():
        raise AbcTimingError(f"Liberty file does not exist: {liberty}")
    liberty_metadata, cell_areas, cell_flags = _inspect_liberty(liberty)
    _verify_inputs(configuration, technology, liberty_metadata, cell_flags)

    preserved_technology_dir = output / "technology"
    preserved_configuration_dir = output / "configuration"
    preserved_constraints_dir = output / "constraints"
    for directory in (
        preserved_technology_dir,
        preserved_configuration_dir,
        preserved_constraints_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    preserved_technology_config = preserved_technology_dir / "technology.json"
    preserved_liberty = preserved_technology_dir / "technology.lib"
    preserved_configuration = preserved_configuration_dir / "area_delay.json"
    preserved_constraints = preserved_constraints_dir / "abc.constr"
    preserved_manifest = output / "source_matched_manifest.json"
    shutil.copyfile(technology_path, preserved_technology_config)
    shutil.copyfile(liberty, preserved_liberty)
    shutil.copyfile(configuration_path, preserved_configuration)
    shutil.copyfile(matched_manifest_path, preserved_manifest)
    preserved_constraints.write_text(
        _constraints_text(
            configuration["io"]["driving_cell"],
            float(configuration["io"]["output_load_femtofarads"]),
        ),
        encoding="utf-8",
    )

    backend_specs = matched_manifest.get("backends")
    expected_hashes = matched_manifest.get("artifact_sha256")
    contract = matched_manifest.get("contract")
    if not isinstance(backend_specs, dict) or not backend_specs:
        raise AbcTimingError("matched manifest has no backend specifications")
    if not isinstance(expected_hashes, dict):
        raise AbcTimingError("matched manifest artifact hashes are malformed")
    if not isinstance(contract, dict):
        raise AbcTimingError("matched manifest contract is malformed")

    input_bits = contract.get("input_count", 0) * contract.get("input_width", 0)
    output_bits = contract.get("output_count", 0) * contract.get(
        "accumulator_width", 0
    )
    if type(input_bits) is not int or input_bits <= 0:
        raise AbcTimingError("matched input bus width is invalid")
    if type(output_bits) is not int or output_bits <= 0:
        raise AbcTimingError("matched output bus width is invalid")

    executable = _resolve_executable(yosys)
    version = _tool_version(executable)
    targets = configuration["targets_picoseconds"]
    backend_evidence: dict[str, Any] = {}

    for backend_name in sorted(backend_specs):
        specification = backend_specs[backend_name]
        if not isinstance(specification, dict):
            raise AbcTimingError(
                f"backend specification {backend_name!r} is malformed"
            )
        module = _validate_module_name(str(specification.get("module", "")))
        rtl_value = specification.get("rtl")
        if not isinstance(rtl_value, str) or not rtl_value:
            raise AbcTimingError(
                f"backend {backend_name!r} does not identify its RTL"
            )
        source_rtl = _resolve_bundle_artifact(bundle, rtl_value)
        hash_label = _EXPECTED_BACKEND_HASH_LABELS.get(backend_name)
        if hash_label is not None:
            expected_hash = expected_hashes.get(hash_label)
            if not isinstance(expected_hash, str) or sha256_file(source_rtl) != expected_hash:
                raise AbcTimingError(
                    f"backend {backend_name!r} RTL hash does not match the manifest"
                )

        runs: dict[str, Any] = {}
        for target in targets:
            label = _target_label(target)
            result = _run_mapping(
                source_rtl=source_rtl,
                module=module,
                target_picoseconds=target,
                run_dir=output / "runs" / backend_name / label,
                executable=executable,
                cell_areas=cell_areas,
                expected_input_bits=input_bits,
                expected_output_bits=output_bits,
                timeout_seconds=timeout_seconds,
            )
            repeatability: dict[str, Any] = {
                "performed": False,
                "passed": False,
            }
            repeated_artifacts: dict[str, Path] = {}
            if verify_repeatability:
                repeatability, repeated_artifacts = _verify_repeatability(
                    source_rtl=source_rtl,
                    module=module,
                    target_picoseconds=target,
                    output_dir=output,
                    backend_name=backend_name,
                    label=label,
                    executable=executable,
                    cell_areas=cell_areas,
                    expected_input_bits=input_bits,
                    expected_output_bits=output_bits,
                    timeout_seconds=timeout_seconds,
                    first_run=result,
                )
            runs[label] = {
                key: value
                for key, value in result.items()
                if key != "artifacts"
            }
            runs[label]["repeatability"] = repeatability
            runs[label]["artifacts"] = _artifact_entries(
                result["artifacts"], output
            )
            runs[label]["repeatability_artifacts"] = _artifact_entries(
                repeated_artifacts, output
            )

        backend_evidence[backend_name] = {
            "module": module,
            "source_rtl": rtl_value,
            "runs": runs,
            "pareto_labels": _pareto_labels(runs),
            "minimum_area_label": _minimum_label(runs, "library_area"),
            "minimum_delay_label": _minimum_label(
                runs, "critical_path_delay_picoseconds"
            ),
        }

    manifest = {
        "schema": "hephaestus.abc-area-delay-evidence.v1",
        "evidence_level": "abc_liberty_area_delay_estimate",
        "source": {
            "matched_manifest": preserved_manifest.name,
            "matched_manifest_sha256": sha256_file(preserved_manifest),
        },
        "technology": {
            "technology_id": technology["technology_id"],
            "library": technology["library"],
            "inspected_liberty": liberty_metadata,
            "configuration_artifact": {
                "path": preserved_technology_config.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_technology_config),
            },
            "liberty_artifact": {
                "path": preserved_liberty.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_liberty),
            },
        },
        "configuration": {
            **configuration,
            "artifact": {
                "path": preserved_configuration.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_configuration),
            },
            "constraints_artifact": {
                "path": preserved_constraints.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_constraints),
            },
        },
        "tool": {
            "name": "Yosys with ABC",
            "requested_executable": yosys,
            "version": version,
        },
        "flow": {
            "script_template": _build_script("TOP", None).replace(
                "-top TOP", "-top <module>"
            ),
            "targeted_script_template": _build_script("TOP", 1234)
            .replace("-top TOP", "-top <module>")
            .replace("-D 1234", "-D <picoseconds>"),
            "abc_terminal_report": "stime -p",
            "input_driver_model": configuration["io"]["driving_cell"],
            "output_load_femtofarads": configuration["io"][
                "output_load_femtofarads"
            ],
            "clock_constraint": None,
            "placement": False,
            "routing": False,
            "parasitic_extraction": False,
        },
        "contract": contract,
        "backends": backend_evidence,
        "comparisons_to_shared_dag": _comparisons_to_shared(backend_evidence),
        "claims": {
            "matched_integer_contract_verified": True,
            "technology_aware_abc_mapping_performed": True,
            "declared_input_driver_model_used": True,
            "declared_output_load_used": True,
            "abc_internal_timing_estimated": True,
            "abc_delay_targets_swept": True,
            "target_attainment_evaluated": True,
            "mapped_netlist_structurally_checked": True,
            "post_mapping_library_area_estimated": True,
            "area_delay_product_computed": True,
            "mapped_gate_level_equivalence_verified": False,
            "signoff_sta_performed": False,
            "sdc_timing_analyzed": False,
            "timing_closed": False,
            "power_estimated": False,
            "placement_performed": False,
            "routing_performed": False,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "abc_area_delay_evidence.json", manifest)
    _write_summary(output / "SUMMARY.md", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep technology-aware ABC area-delay points for verified matched RTL."
        )
    )
    parser.add_argument("matched_bundle", type=Path)
    parser.add_argument("--technology", type=Path, required=True)
    parser.add_argument("--liberty", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/abc-area-delay"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--verify-repeatability", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_abc_area_delay_evidence(
            arguments.matched_bundle,
            arguments.technology,
            arguments.liberty,
            arguments.config,
            arguments.out,
            yosys=arguments.yosys,
            verify_repeatability=arguments.verify_repeatability,
            timeout_seconds=arguments.timeout,
        )
    except (
        AbcTimingError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"built {len(manifest['backends'])} ABC area-delay backend sweeps at "
        f"evidence level {manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
