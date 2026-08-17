"""Reproducible generic Yosys evidence for matched Hephaestus RTL backends."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .report import sha256_file, write_json


class SynthesisError(RuntimeError):
    """Raised when a synthesis evidence bundle cannot be produced safely."""


_EXPECTED_BACKEND_HASH_LABELS = {
    "shared_dag": "shared_dag_rtl",
    "naive_shift_add": "naive_shift_add_rtl",
    "constant_multipliers": "constant_multiplier_rtl",
}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesisError(f"cannot read JSON artifact {path}: {exc}") from exc


def _validate_module_name(module: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", module) is None:
        raise SynthesisError(f"unsafe or unsupported SystemVerilog module name: {module!r}")
    return module


def _resolve_bundle_artifact(bundle_dir: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise SynthesisError(f"bundle artifact path must be relative: {raw_path!r}")

    root = bundle_dir.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SynthesisError(f"bundle artifact escapes its root: {raw_path!r}") from exc
    if not resolved.is_file():
        raise SynthesisError(f"bundle artifact does not exist: {resolved}")
    return resolved


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise SynthesisError(f"Yosys executable was not found: {requested!r}")
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
        raise SynthesisError(f"cannot identify Yosys version using {executable!r}")
    return output.splitlines()[0]


def _build_script(module: str) -> str:
    top = _validate_module_name(module)
    commands = [
        "read_verilog -sv input.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "opt",
        "flatten",
        "opt",
        "check",
        "tee -o pre_techmap.stat.txt stat",
        "write_json pre_techmap.netlist.json",
        "techmap",
        "opt",
        "clean -purge",
        "check",
        "tee -o post_techmap.stat.txt stat",
        "write_json post_techmap.netlist.json",
    ]
    return "\n".join(commands) + "\n"


def _integer_bits(values: list[Any]) -> set[int]:
    return {value for value in values if type(value) is int}


def _netlist_metrics(netlist: Any, module: str) -> dict[str, Any]:
    if not isinstance(netlist, dict):
        raise SynthesisError("Yosys netlist root must be a JSON object")
    modules = netlist.get("modules")
    if not isinstance(modules, dict) or module not in modules:
        raise SynthesisError(f"Yosys netlist does not contain top module {module!r}")
    top = modules[module]
    if not isinstance(top, dict):
        raise SynthesisError(f"Yosys top module {module!r} is malformed")

    cells = top.get("cells", {})
    ports = top.get("ports", {})
    netnames = top.get("netnames", {})
    memories = top.get("memories", {})
    if not all(isinstance(value, dict) for value in (cells, ports, netnames, memories)):
        raise SynthesisError("Yosys netlist contains malformed module dictionaries")

    cell_types: Counter[str] = Counter()
    connection_bits = 0
    unique_signal_bits: set[int] = set()
    for cell in cells.values():
        if not isinstance(cell, dict):
            raise SynthesisError("Yosys cell entry must be a JSON object")
        cell_type = cell.get("type")
        if not isinstance(cell_type, str):
            raise SynthesisError("Yosys cell entry is missing its type")
        cell_types[cell_type] += 1
        connections = cell.get("connections", {})
        if not isinstance(connections, dict):
            raise SynthesisError("Yosys cell connections must be a JSON object")
        for bits in connections.values():
            if not isinstance(bits, list):
                raise SynthesisError("Yosys cell connection bits must be a list")
            connection_bits += len(bits)
            unique_signal_bits.update(_integer_bits(bits))

    port_bits: Counter[str] = Counter()
    for port in ports.values():
        if not isinstance(port, dict):
            raise SynthesisError("Yosys port entry must be a JSON object")
        direction = port.get("direction")
        bits = port.get("bits")
        if direction not in {"input", "output", "inout"} or not isinstance(bits, list):
            raise SynthesisError("Yosys port entry is malformed")
        port_bits[str(direction)] += len(bits)
        unique_signal_bits.update(_integer_bits(bits))

    for netname in netnames.values():
        if not isinstance(netname, dict):
            raise SynthesisError("Yosys netname entry must be a JSON object")
        bits = netname.get("bits")
        if not isinstance(bits, list):
            raise SynthesisError("Yosys netname bits must be a list")
        unique_signal_bits.update(_integer_bits(bits))

    histogram = dict(sorted(cell_types.items()))
    return {
        "module_count": len(modules),
        "cell_count": len(cells),
        "cell_type_count": len(histogram),
        "cell_type_histogram": histogram,
        "generic_internal_cell_count": sum(
            count for cell_type, count in cell_types.items() if cell_type.startswith("$_")
        ),
        "abstract_operator_cell_count": sum(
            count
            for cell_type, count in cell_types.items()
            if cell_type.startswith("$") and not cell_type.startswith("$_")
        ),
        "port_count": len(ports),
        "input_bits": port_bits["input"],
        "output_bits": port_bits["output"],
        "inout_bits": port_bits["inout"],
        "netname_count": len(netnames),
        "unique_signal_bits": len(unique_signal_bits),
        "cell_connection_bits": connection_bits,
        "memory_count": len(memories),
    }


def _required_run_artifacts(run_dir: Path) -> dict[str, Path]:
    return {
        "input_rtl": run_dir / "input.sv",
        "script": run_dir / "synthesis.ys",
        "stdout": run_dir / "yosys.stdout.txt",
        "stderr": run_dir / "yosys.stderr.txt",
        "pre_techmap_stat": run_dir / "pre_techmap.stat.txt",
        "pre_techmap_netlist": run_dir / "pre_techmap.netlist.json",
        "post_techmap_stat": run_dir / "post_techmap.stat.txt",
        "post_techmap_netlist": run_dir / "post_techmap.netlist.json",
    }


def _run_yosys(
    *,
    source_rtl: Path,
    module: str,
    run_dir: Path,
    executable: str,
) -> dict[str, Any]:
    top = _validate_module_name(module)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_rtl, run_dir / "input.sv")
    (run_dir / "synthesis.ys").write_text(_build_script(top), encoding="utf-8")

    completed = subprocess.run(
        [executable, "-s", "synthesis.ys"],
        cwd=run_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    (run_dir / "yosys.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (run_dir / "yosys.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise SynthesisError(f"Yosys failed for backend {run_dir.name!r}; inspect yosys.stderr.txt")

    artifacts = _required_run_artifacts(run_dir)
    missing = [label for label, path in artifacts.items() if not path.is_file()]
    if missing:
        raise SynthesisError(
            f"Yosys did not produce required artifacts for {run_dir.name!r}: {missing}"
        )

    pre_netlist = _load_json(artifacts["pre_techmap_netlist"])
    post_netlist = _load_json(artifacts["post_techmap_netlist"])
    return {
        "pre_techmap": _netlist_metrics(pre_netlist, top),
        "post_techmap": _netlist_metrics(post_netlist, top),
        "artifacts": artifacts,
    }


def _verify_repeatability(
    *,
    source_rtl: Path,
    module: str,
    executable: str,
    first_run: dict[str, Any],
    temporary_parent: Path,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hephaestus-yosys-", dir=temporary_parent) as raw:
        second = _run_yosys(
            source_rtl=source_rtl,
            module=module,
            run_dir=Path(raw),
            executable=executable,
        )
        compared = ("pre_techmap_netlist", "post_techmap_netlist")
        matches = {
            label: sha256_file(first_run["artifacts"][label])
            == sha256_file(second["artifacts"][label])
            for label in compared
        }
        metrics_match = (
            first_run["pre_techmap"] == second["pre_techmap"]
            and first_run["post_techmap"] == second["post_techmap"]
        )
        passed = all(matches.values()) and metrics_match
        if not passed:
            raise SynthesisError(
                f"generic synthesis is not repeatable for module {module!r}: "
                f"netlists={matches}, metrics={metrics_match}"
            )
        return {
            "performed": True,
            "passed": True,
            "byte_identical_netlists": matches,
            "normalized_metrics_identical": metrics_match,
        }


def build_synthesis_evidence(
    matched_bundle: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    verify_repeatability: bool = False,
) -> dict[str, Any]:
    """Run one pinned generic Yosys flow for every verified matched backend."""

    bundle = matched_bundle.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    matched_manifest_path = bundle / "matched_manifest.json"
    if not matched_manifest_path.is_file():
        raise SynthesisError(f"matched bundle is missing {matched_manifest_path.name}")
    matched_manifest = _load_json(matched_manifest_path)
    preserved_manifest = output / "source_matched_manifest.json"
    shutil.copyfile(matched_manifest_path, preserved_manifest)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise SynthesisError("unsupported matched-baseline manifest schema")
    if not matched_manifest.get("claims", {}).get("matched_integer_contract_verified"):
        raise SynthesisError("matched integer contract must be verified before synthesis evidence")

    backend_specs = matched_manifest.get("backends")
    if not isinstance(backend_specs, dict) or not backend_specs:
        raise SynthesisError("matched manifest does not contain backend specifications")

    executable = _resolve_executable(yosys)
    version = _tool_version(executable)
    expected_hashes = matched_manifest.get("artifact_sha256", {})
    if not isinstance(expected_hashes, dict):
        raise SynthesisError("matched manifest artifact hashes are malformed")

    backend_evidence: dict[str, Any] = {}
    for backend_name in sorted(backend_specs):
        specification = backend_specs[backend_name]
        if not isinstance(specification, dict):
            raise SynthesisError(f"backend specification {backend_name!r} is malformed")
        module = _validate_module_name(str(specification.get("module", "")))
        rtl_value = specification.get("rtl")
        if not isinstance(rtl_value, str) or not rtl_value:
            raise SynthesisError(f"backend {backend_name!r} does not identify its RTL")
        source_rtl = _resolve_bundle_artifact(bundle, rtl_value)

        hash_label = _EXPECTED_BACKEND_HASH_LABELS.get(backend_name)
        if hash_label is not None and hash_label in expected_hashes:
            actual_hash = sha256_file(source_rtl)
            if actual_hash != expected_hashes[hash_label]:
                raise SynthesisError(
                    f"backend {backend_name!r} RTL hash does not match the matched manifest"
                )

        run_dir = output / backend_name
        result = _run_yosys(
            source_rtl=source_rtl,
            module=module,
            run_dir=run_dir,
            executable=executable,
        )
        repeatability: dict[str, Any] = {"performed": False, "passed": False}
        if verify_repeatability:
            repeatability = _verify_repeatability(
                source_rtl=source_rtl,
                module=module,
                executable=executable,
                first_run=result,
                temporary_parent=output,
            )

        artifact_entries = {
            label: {
                "path": path.relative_to(output).as_posix(),
                "sha256": sha256_file(path),
            }
            for label, path in result["artifacts"].items()
        }
        backend_evidence[backend_name] = {
            "module": module,
            "source_rtl": rtl_value,
            "pre_techmap": result["pre_techmap"],
            "post_techmap": result["post_techmap"],
            "repeatability": repeatability,
            "artifacts": artifact_entries,
        }

    manifest = {
        "schema": "hephaestus.generic-yosys-evidence.v1",
        "evidence_level": "generic_yosys_post_techmap",
        "source": {
            "matched_manifest": preserved_manifest.name,
            "matched_manifest_sha256": sha256_file(preserved_manifest),
        },
        "tool": {
            "name": "Yosys",
            "requested_executable": yosys,
            "version": version,
        },
        "flow": {
            "script_template": _build_script("TOP").replace("-top TOP", "-top <module>"),
            "passes": [
                "read_verilog",
                "hierarchy",
                "proc",
                "opt",
                "flatten",
                "check",
                "stat",
                "write_json",
                "techmap",
                "clean",
            ],
            "standard_cell_library": None,
            "timing_constraints": None,
            "physical_design": False,
        },
        "backends": backend_evidence,
        "claims": {
            "matched_integer_contract_verified": True,
            "generic_yosys_synthesis_completed": True,
            "standard_cell_mapping_performed": False,
            "timing_constrained": False,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "synthesis_evidence.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build normalized generic Yosys evidence for matched RTL backends."
    )
    parser.add_argument("matched_bundle", type=Path)
    parser.add_argument("--out", type=Path, default=Path("build/synthesis-evidence"))
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--verify-repeatability", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_synthesis_evidence(
            arguments.matched_bundle,
            arguments.out,
            yosys=arguments.yosys,
            verify_repeatability=arguments.verify_repeatability,
        )
    except (OSError, subprocess.SubprocessError, SynthesisError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"synthesized {len(manifest['backends'])} matched backends at evidence level "
        f"{manifest['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
