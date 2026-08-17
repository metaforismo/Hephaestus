"""Pinned standard-cell mapping evidence for matched Hephaestus RTL backends."""

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


class MappedSynthesisError(RuntimeError):
    """Raised when mapped synthesis evidence cannot be produced safely."""


_EXPECTED_BACKEND_HASH_LABELS = {
    "shared_dag": "shared_dag_rtl",
    "naive_shift_add": "naive_shift_add_rtl",
    "constant_multipliers": "constant_multiplier_rtl",
}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappedSynthesisError(f"cannot read JSON artifact {path}: {exc}") from exc


def _validate_module_name(module: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise MappedSynthesisError(f"unsafe or unsupported SystemVerilog module name: {module!r}")
    return module


def _resolve_bundle_artifact(bundle_dir: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise MappedSynthesisError(f"bundle artifact path must be relative: {raw_path!r}")

    root = bundle_dir.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MappedSynthesisError(f"bundle artifact escapes its root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise MappedSynthesisError(f"bundle artifact does not exist: {resolved}")
    return resolved


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise MappedSynthesisError(f"Yosys executable was not found: {requested!r}")
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
        raise MappedSynthesisError(f"cannot identify Yosys version using {executable!r}")
    return output.splitlines()[0]


def _required_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MappedSynthesisError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _required_number(mapping: dict[str, Any], key: str, context: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MappedSynthesisError(f"{context}.{key} must be numeric")
    return float(value)


def _load_technology_config(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise MappedSynthesisError("technology configuration must be a JSON object")
    if raw.get("schema") != "hephaestus.technology.v1":
        raise MappedSynthesisError("unsupported technology configuration schema")

    _required_string(raw, "technology_id", "technology")
    _required_string(raw, "provider", "technology")
    _required_string(raw, "process", "technology")

    library = raw.get("library")
    source = raw.get("source")
    flow = raw.get("flow")
    if not isinstance(library, dict):
        raise MappedSynthesisError("technology.library must be a JSON object")
    if not isinstance(source, dict):
        raise MappedSynthesisError("technology.source must be a JSON object")
    if not isinstance(flow, dict):
        raise MappedSynthesisError("technology.flow must be a JSON object")

    _required_string(library, "name", "technology.library")
    _required_string(library, "corner", "technology.library")
    _required_string(library, "area_unit", "technology.library")
    sha256 = _required_string(library, "sha256", "technology.library")
    if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise MappedSynthesisError("technology.library.sha256 must be a lowercase SHA-256 digest")
    byte_count = library.get("bytes")
    if type(byte_count) is not int or byte_count <= 0:
        raise MappedSynthesisError("technology.library.bytes must be a positive integer")
    _required_number(library, "nominal_voltage", "technology.library")
    _required_number(library, "nominal_temperature_c", "technology.library")

    for key in ("repository", "commit", "path", "url"):
        _required_string(source, key, "technology.source")
    commit = str(source["commit"])
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise MappedSynthesisError("technology.source.commit must be a full lowercase Git commit")
    _required_string(flow, "mapper", "technology.flow")
    if flow.get("physical_design") is not False:
        raise MappedSynthesisError(
            "mapped-synthesis technology configurations must keep physical_design false"
        )
    if flow.get("timing_constraint") is not None:
        raise MappedSynthesisError(
            "this evidence level currently requires timing_constraint to be null"
        )
    return raw


def _inspect_liberty(path: Path) -> tuple[dict[str, Any], dict[str, float]]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MappedSynthesisError(f"cannot read Liberty file {path}: {exc}") from exc

    library_match = re.search(r"library\s*\(\s*([^\s)]+)\s*\)", text)
    voltage_match = re.search(r"nom_voltage\s*:\s*([^;]+);", text)
    temperature_match = re.search(r"nom_temperature\s*:\s*([^;]+);", text)
    cell_matches = list(
        re.finditer(
            r"^\s*cell\s*\(\s*([^\s)]+)\s*\)\s*\{",
            text,
            re.MULTILINE,
        )
    )
    if library_match is None or not cell_matches:
        raise MappedSynthesisError("Liberty file is missing its library or cell declarations")

    cell_areas: dict[str, float] = {}
    for index, match in enumerate(cell_matches):
        end = cell_matches[index + 1].start() if index + 1 < len(cell_matches) else len(text)
        body = text[match.end() : end]
        area_match = re.search(
            r"^\s*area\s*:\s*([0-9.eE+-]+)\s*;",
            body,
            re.MULTILINE,
        )
        if area_match is None:
            raise MappedSynthesisError(f"Liberty cell {match.group(1)!r} does not declare an area")
        area = float(area_match.group(1))
        if not math.isfinite(area) or area < 0:
            raise MappedSynthesisError(f"Liberty cell {match.group(1)!r} has an invalid area")
        cell_areas[match.group(1)] = area

    try:
        voltage = float(voltage_match.group(1)) if voltage_match else None
        temperature = float(temperature_match.group(1)) if temperature_match else None
    except ValueError as exc:
        raise MappedSynthesisError("Liberty nominal conditions are not numeric") from exc

    metadata = {
        "sha256": sha256_file(path),
        "bytes": len(raw),
        "library": library_match.group(1),
        "nominal_voltage": voltage,
        "nominal_temperature_c": temperature,
        "cell_declarations": len(cell_matches),
        "cells_with_area": len(cell_areas),
        "minimum_cell_area": min(cell_areas.values()),
        "maximum_cell_area": max(cell_areas.values()),
    }
    return metadata, cell_areas


def _verify_technology(
    config: dict[str, Any],
    liberty_metadata: dict[str, Any],
) -> None:
    library = config["library"]
    comparisons = {
        "name": (library["name"], liberty_metadata["library"]),
        "sha256": (library["sha256"], liberty_metadata["sha256"]),
        "bytes": (library["bytes"], liberty_metadata["bytes"]),
    }
    for label, (expected, actual) in comparisons.items():
        if expected != actual:
            raise MappedSynthesisError(
                f"Liberty {label} does not match the pinned technology configuration: "
                f"expected {expected!r}, got {actual!r}"
            )

    numeric_comparisons = {
        "nominal_voltage": (
            float(library["nominal_voltage"]),
            liberty_metadata["nominal_voltage"],
        ),
        "nominal_temperature_c": (
            float(library["nominal_temperature_c"]),
            liberty_metadata["nominal_temperature_c"],
        ),
    }
    for label, (expected, actual) in numeric_comparisons.items():
        if actual is None or not math.isclose(expected, float(actual), abs_tol=1e-12):
            raise MappedSynthesisError(
                f"Liberty {label} does not match the pinned technology configuration: "
                f"expected {expected!r}, got {actual!r}"
            )


def _build_script(module: str, liberty_path: str = "../../technology/technology.lib") -> str:
    top = _validate_module_name(module)
    if re.fullmatch(r"[A-Za-z0-9_./-]+", liberty_path) is None:
        raise MappedSynthesisError(f"unsafe Liberty path in mapping script: {liberty_path!r}")
    commands = [
        f"read_liberty -lib {liberty_path}",
        "read_verilog -sv input.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        "opt",
        "techmap",
        "opt",
        f"abc -liberty {liberty_path}",
        "clean -purge",
        "check -assert",
        f"tee -o mapped.stat.txt stat -liberty {liberty_path}",
        "write_verilog -noattr mapped.v",
        "write_json mapped.json",
    ]
    return "\n".join(commands) + "\n"


def _parse_library_area(stat_text: str, module: str) -> float:
    top = re.escape(module)
    matches = re.findall(
        rf"Chip area for module '\\?{top}':\s*([0-9.eE+-]+)",
        stat_text,
    )
    if len(matches) != 1:
        raise MappedSynthesisError(
            f"expected one mapped area for module {module!r}, found {len(matches)}"
        )
    area = float(matches[0])
    if not math.isfinite(area) or area <= 0:
        raise MappedSynthesisError(f"mapped area for module {module!r} is invalid")
    return area


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
    run_dir: Path,
    executable: str,
    cell_areas: dict[str, float],
    timeout_seconds: int,
) -> dict[str, Any]:
    top = _validate_module_name(module)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_rtl, run_dir / "input.sv")
    (run_dir / "map.ys").write_text(_build_script(top), encoding="utf-8")

    completed = subprocess.run(
        [executable, "-s", "map.ys"],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    (run_dir / "yosys.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (run_dir / "yosys.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise MappedSynthesisError(
            f"Yosys mapping failed for backend {run_dir.name!r}; inspect preserved logs"
        )

    artifacts = _required_run_artifacts(run_dir)
    missing = [label for label, path in artifacts.items() if not path.is_file()]
    if missing:
        raise MappedSynthesisError(
            f"Yosys did not produce required mapped artifacts for {run_dir.name!r}: {missing}"
        )

    netlist = _load_json(artifacts["mapped_netlist"])
    metrics = _netlist_metrics(netlist, top)
    histogram = metrics["cell_type_histogram"]
    unknown_cells = sorted(set(histogram) - set(cell_areas))
    if unknown_cells:
        raise MappedSynthesisError(
            f"backend {run_dir.name!r} contains cells absent from the pinned Liberty: "
            f"{unknown_cells}"
        )
    if metrics["cell_count"] <= 0:
        raise MappedSynthesisError(f"backend {run_dir.name!r} mapped to no cells")

    stat_text = artifacts["mapped_stat"].read_text(encoding="utf-8")
    reported_area = _parse_library_area(stat_text, top)
    calculated_area = sum(cell_areas[cell] * count for cell, count in histogram.items())
    if not math.isclose(reported_area, calculated_area, rel_tol=1e-12, abs_tol=1e-6):
        raise MappedSynthesisError(
            f"mapped area cross-check failed for {run_dir.name!r}: "
            f"Yosys={reported_area}, histogram={calculated_area}"
        )

    return {
        "metrics": metrics,
        "library_area": reported_area,
        "library_area_from_histogram": calculated_area,
        "area_cross_check_passed": True,
        "structural_check": "yosys check -assert",
        "artifacts": artifacts,
    }


def _verify_repeatability(
    *,
    source_rtl: Path,
    module: str,
    output_dir: Path,
    backend_name: str,
    executable: str,
    cell_areas: dict[str, float],
    timeout_seconds: int,
    first_run: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Path]]:
    second = _run_mapping(
        source_rtl=source_rtl,
        module=module,
        run_dir=output_dir / "repeatability" / backend_name,
        executable=executable,
        cell_areas=cell_areas,
        timeout_seconds=timeout_seconds,
    )
    compared = ("mapped_netlist", "mapped_verilog", "mapped_stat")
    byte_identical = {
        label: sha256_file(first_run["artifacts"][label]) == sha256_file(second["artifacts"][label])
        for label in compared
    }
    normalized_match = first_run["metrics"] == second["metrics"] and math.isclose(
        first_run["library_area"],
        second["library_area"],
        rel_tol=0,
        abs_tol=1e-12,
    )
    passed = all(byte_identical.values()) and normalized_match
    if not passed:
        raise MappedSynthesisError(
            f"mapped synthesis is not repeatable for backend {backend_name!r}: "
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


def _comparisons(backends: dict[str, Any]) -> dict[str, Any]:
    if "shared_dag" not in backends:
        return {}
    shared_area = float(backends["shared_dag"]["library_area"])
    shared_cells = int(backends["shared_dag"]["metrics"]["cell_count"])
    comparisons: dict[str, Any] = {}
    for name, backend in sorted(backends.items()):
        if name == "shared_dag":
            continue
        area = float(backend["library_area"])
        cells = int(backend["metrics"]["cell_count"])
        comparisons[name] = {
            "shared_dag_area_difference": area - shared_area,
            "shared_dag_area_ratio": shared_area / area,
            "shared_dag_area_reduction_percent": 100.0 * (area - shared_area) / area,
            "shared_dag_cell_difference": cells - shared_cells,
            "shared_dag_cell_reduction_percent": 100.0 * (cells - shared_cells) / cells,
        }
    return comparisons


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    technology = manifest["technology"]
    lines = [
        "# Standard-cell mapped synthesis evidence",
        "",
        f"Technology: `{technology['technology_id']}`",
        "",
        f"Liberty: `{technology['library']['name']}` at "
        f"{technology['library']['nominal_voltage']} V and "
        f"{technology['library']['nominal_temperature_c']} °C",
        "",
        "| Backend | Cells | Library area | Repeatable |",
        "|---|---:|---:|:---:|",
    ]
    for name, backend in sorted(manifest["backends"].items()):
        lines.append(
            f"| `{name}` | {backend['metrics']['cell_count']} | "
            f"{backend['library_area']:.6f} | "
            f"{'yes' if backend['repeatability']['passed'] else 'not run'} |"
        )
    lines.extend(
        [
            "",
            "The area values are sums of Liberty cell `area` attributes. They are not placed die "
            "area, routed area, timing, power, or silicon measurements.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_mapped_synthesis_evidence(
    matched_bundle: Path,
    technology_config: Path,
    liberty_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    verify_repeatability: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Map every verified backend through one pinned standard-cell Liberty file."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    bundle = matched_bundle.resolve()
    config_path = technology_config.resolve()
    liberty = liberty_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    matched_manifest_path = bundle / "matched_manifest.json"
    if not matched_manifest_path.is_file():
        raise MappedSynthesisError(f"matched bundle is missing {matched_manifest_path.name}")
    matched_manifest = _load_json(matched_manifest_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise MappedSynthesisError("unsupported matched-baseline manifest schema")
    if not matched_manifest.get("claims", {}).get("matched_integer_contract_verified"):
        raise MappedSynthesisError(
            "matched integer contract must be verified before standard-cell mapping"
        )

    config = _load_technology_config(config_path)
    if not liberty.is_file():
        raise MappedSynthesisError(f"Liberty file does not exist: {liberty}")
    liberty_metadata, cell_areas = _inspect_liberty(liberty)
    _verify_technology(config, liberty_metadata)

    technology_dir = output / "technology"
    technology_dir.mkdir(parents=True, exist_ok=True)
    preserved_config = technology_dir / "technology.json"
    preserved_liberty = technology_dir / "technology.lib"
    shutil.copyfile(config_path, preserved_config)
    shutil.copyfile(liberty, preserved_liberty)
    preserved_manifest = output / "source_matched_manifest.json"
    shutil.copyfile(matched_manifest_path, preserved_manifest)

    backend_specs = matched_manifest.get("backends")
    if not isinstance(backend_specs, dict) or not backend_specs:
        raise MappedSynthesisError("matched manifest does not contain backend specifications")
    expected_hashes = matched_manifest.get("artifact_sha256", {})
    if not isinstance(expected_hashes, dict):
        raise MappedSynthesisError("matched manifest artifact hashes are malformed")

    executable = _resolve_executable(yosys)
    version = _tool_version(executable)
    contract = matched_manifest.get("contract", {})
    if not isinstance(contract, dict):
        raise MappedSynthesisError("matched manifest contract is malformed")

    backend_evidence: dict[str, Any] = {}
    for backend_name in sorted(backend_specs):
        specification = backend_specs[backend_name]
        if not isinstance(specification, dict):
            raise MappedSynthesisError(f"backend specification {backend_name!r} is malformed")
        module = _validate_module_name(str(specification.get("module", "")))
        rtl_value = specification.get("rtl")
        if not isinstance(rtl_value, str) or not rtl_value:
            raise MappedSynthesisError(f"backend {backend_name!r} does not identify its RTL")
        source_rtl = _resolve_bundle_artifact(bundle, rtl_value)

        hash_label = _EXPECTED_BACKEND_HASH_LABELS.get(backend_name)
        if hash_label is not None:
            expected_hash = expected_hashes.get(hash_label)
            if not isinstance(expected_hash, str) or sha256_file(source_rtl) != expected_hash:
                raise MappedSynthesisError(
                    f"backend {backend_name!r} RTL hash does not match the matched manifest"
                )

        result = _run_mapping(
            source_rtl=source_rtl,
            module=module,
            run_dir=output / "backends" / backend_name,
            executable=executable,
            cell_areas=cell_areas,
            timeout_seconds=timeout_seconds,
        )
        if result["metrics"]["input_bits"] != contract.get("input_count", 0) * contract.get(
            "input_width", 0
        ):
            raise MappedSynthesisError(
                f"mapped backend {backend_name!r} input width differs from the contract"
            )
        if result["metrics"]["output_bits"] != contract.get("output_count", 0) * contract.get(
            "accumulator_width", 0
        ):
            raise MappedSynthesisError(
                f"mapped backend {backend_name!r} output width differs from the contract"
            )

        repeatability: dict[str, Any] = {"performed": False, "passed": False}
        repeatability_artifacts: dict[str, Path] = {}
        if verify_repeatability:
            repeatability, repeatability_artifacts = _verify_repeatability(
                source_rtl=source_rtl,
                module=module,
                output_dir=output,
                backend_name=backend_name,
                executable=executable,
                cell_areas=cell_areas,
                timeout_seconds=timeout_seconds,
                first_run=result,
            )

        artifacts = {
            label: {
                "path": path.relative_to(output).as_posix(),
                "sha256": sha256_file(path),
            }
            for label, path in result["artifacts"].items()
        }
        repeated_artifacts = {
            label: {
                "path": path.relative_to(output).as_posix(),
                "sha256": sha256_file(path),
            }
            for label, path in repeatability_artifacts.items()
        }
        backend_evidence[backend_name] = {
            "module": module,
            "source_rtl": rtl_value,
            "metrics": result["metrics"],
            "library_area": result["library_area"],
            "library_area_unit": config["library"]["area_unit"],
            "library_area_from_histogram": result["library_area_from_histogram"],
            "area_cross_check_passed": result["area_cross_check_passed"],
            "structural_check": result["structural_check"],
            "repeatability": repeatability,
            "artifacts": artifacts,
            "repeatability_artifacts": repeated_artifacts,
        }

    manifest = {
        "schema": "hephaestus.standard-cell-mapped-evidence.v1",
        "evidence_level": "standard_cell_mapped_area_estimate",
        "source": {
            "matched_manifest": preserved_manifest.name,
            "matched_manifest_sha256": sha256_file(preserved_manifest),
        },
        "technology": {
            **config,
            "configuration_artifact": {
                "path": preserved_config.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_config),
            },
            "liberty_artifact": {
                "path": preserved_liberty.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_liberty),
            },
            "inspected_liberty": liberty_metadata,
        },
        "tool": {
            "name": "Yosys with ABC",
            "requested_executable": yosys,
            "version": version,
        },
        "flow": {
            "script_template": _build_script("TOP").replace("-top TOP", "-top <module>"),
            "passes": [
                "read_liberty",
                "read_verilog",
                "hierarchy",
                "proc",
                "flatten",
                "opt",
                "techmap",
                "abc",
                "clean",
                "check -assert",
                "stat -liberty",
                "write_verilog",
                "write_json",
            ],
            "timing_constraint": None,
            "placement": False,
            "routing": False,
            "parasitic_extraction": False,
        },
        "contract": contract,
        "backends": backend_evidence,
        "comparisons_to_shared_dag": _comparisons(backend_evidence),
        "claims": {
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": True,
            "mapped_netlist_structurally_checked": True,
            "post_mapping_library_area_estimated": True,
            "mapped_gate_level_equivalence_verified": False,
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
    write_json(output / "mapped_evidence.json", manifest)
    _write_summary(output / "SUMMARY.md", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Map verified matched backends through one pinned Liberty library."
    )
    parser.add_argument("matched_bundle", type=Path)
    parser.add_argument("--technology", type=Path, required=True)
    parser.add_argument("--liberty", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/mapped-evidence"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--verify-repeatability", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_mapped_synthesis_evidence(
            arguments.matched_bundle,
            arguments.technology,
            arguments.liberty,
            arguments.out,
            yosys=arguments.yosys,
            verify_repeatability=arguments.verify_repeatability,
            timeout_seconds=arguments.timeout,
        )
    except (
        MappedSynthesisError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"mapped {len(manifest['backends'])} backends at evidence level "
        f"{manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
