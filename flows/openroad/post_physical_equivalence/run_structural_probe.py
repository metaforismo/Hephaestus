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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
SUCCESS_MARKER = "Equivalence successfully proven!"


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


def emit_script(top: str, *, mode: str) -> str:
    if mode not in {"simple", "structural", "inductive"}:
        raise ValueError(f"unsupported proof mode: {mode}")
    proof = {
        "simple": ["equiv_simple", "equiv_status -assert"],
        "structural": [
            "equiv_struct -maxiter 20",
            "equiv_simple",
            "equiv_status -assert",
        ],
        "inductive": [
            "equiv_struct -maxiter 20",
            "equiv_simple",
            "equiv_induct -seq 4",
            "equiv_status -assert",
        ],
    }[mode]
    commands = [
        "# Normalize the exact registered source.",
        "read_verilog -sv source_core.sv source_wrapper.sv",
        f"hierarchy -check -top {top}",
        "proc",
        "async2sync",
        "flatten",
        "memory",
        "opt -full",
        f"rename {top} gold",
        "design -stash gold_design",
        "",
        "# Normalize the routed netlist with functional-only IHP cell models.",
        "read_verilog -sv models.v routed.v",
        f"hierarchy -check -top {top}",
        "proc",
        "async2sync",
        "flatten",
        "memory",
        "opt -full",
        f"rename {top} gate",
        "design -stash gate_design",
        "",
        "# Import both normalized tops into one equivalence design.",
        "design -copy-from gold_design gold",
        "design -copy-from gate_design gate",
        "equiv_make gold gate equiv",
        "hierarchy -check -top equiv",
        "proc",
        "opt -full",
        *proof,
        "",
    ]
    return "\n".join(commands)


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
        "passed": (not timed_out and process.returncode == 0 and SUCCESS_MARKER in combined),
        "success_marker_found": SUCCESS_MARKER in combined,
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

    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for backend in BACKENDS:
        prepared_backend = prepared["backends"][backend]
        physical_backend = physical["backends"][backend]
        runs = physical_backend.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise StructuralProbeError(f"{backend} does not have two physical runs")
        run = next((item for item in runs if item.get("attempt") == 1), None)
        if not isinstance(run, dict):
            raise StructuralProbeError(f"{backend} attempt one is missing")
        routed_meta = run.get("artifacts", {}).get("final_verilog")
        if not isinstance(routed_meta, dict):
            raise StructuralProbeError(f"{backend} routed-Verilog metadata is missing")

        attempt_root = root / "downloaded-runs" / f"openroad-physical-run-{backend}-1"
        routed = exactly_one(attempt_root, "6_final.v")
        expected_routed = routed_meta.get("sha256")
        if sha256(routed) != expected_routed:
            raise StructuralProbeError(f"{backend} routed-Verilog digest mismatch")

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
        shutil.copyfile(routed, backend_dir / "routed.v")
        shutil.copyfile(model_path, backend_dir / "models.v")

        modes: dict[str, Any] = {}
        for mode in ("simple", "structural", "inductive"):
            script = backend_dir / f"{mode}.ys"
            script.write_text(
                emit_script(prepared_backend["wrapper_module"], mode=mode),
                encoding="utf-8",
            )
            modes[mode] = run_command(
                resolved_yosys,
                backend_dir,
                script,
                timeout=timeout,
            )
            if modes[mode]["passed"]:
                break

        results[backend] = {
            "source_core_sha256": sha256(source_core),
            "source_wrapper_sha256": sha256(source_wrapper),
            "routed_verilog_sha256": sha256(routed),
            "modes": modes,
            "passed": any(result["passed"] for result in modes.values()),
        }

    evidence = {
        "schema": "hephaestus.post-physical-structural-probe.v1",
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
        "backends": results,
        "claims": {
            "all_three_exact_registered_sources_proved_against_routed_netlists": all(
                value["passed"] for value in results.values()
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
    if not evidence["claims"]["all_three_exact_registered_sources_proved_against_routed_netlists"]:
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
        f"{evidence['claims']['all_three_exact_registered_sources_proved_against_routed_netlists']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
