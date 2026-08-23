#!/usr/bin/env python3
"""Run a digest-bound multi-corner timing probe on routed Hephaestus tiles.

The probe is intentionally research-only. It consumes the qualified matched
OpenROAD evidence, selects the exact routed Verilog/SDC/SPEF artifacts through
their recorded SHA-256 digests, discovers three official IHP Open PDK Liberty
corners, and runs OpenSTA twice per backend/corner.

It does not claim foundry sign-off, OCV/AOCV/POCV coverage, statistical
variation, extracted-coupling sign-off, or silicon behaviour.
"""

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
CORNER_LABELS = ("slow", "typ", "fast")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
SLACK_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+slack\s+"
    r"\((MET|VIOLATED)\)\s*$",
    re.MULTILINE,
)
TNS_RE = re.compile(
    r"^\s*tns\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
CLOCK_PERIOD_RE = re.compile(
    r"(?P<prefix>\bcreate_clock\b[^\n]*?\s-period\s+)"
    r"(?P<period>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)


class PVTProbeError(RuntimeError):
    """Raised when the PVT probe cannot preserve its evidence contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PVTProbeError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PVTProbeError(f"JSON root must be an object: {path}")
    return value


def require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PVTProbeError(f"{context} must be a lowercase SHA-256 digest")
    return value


def require_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or MODULE_RE.fullmatch(value) is None:
        raise PVTProbeError(f"{context} is not a safe Verilog module name")
    return value


def exactly_one(root: Path, name: str) -> Path:
    matches = sorted(root.rglob(name))
    if len(matches) != 1:
        raise PVTProbeError(
            f"expected exactly one {name!r} below {root}, found {len(matches)}"
        )
    path = matches[0]
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise PVTProbeError(f"invalid required artifact: {path}")
    return path


def resolve_under(root: Path, raw_path: Any, *, context: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise PVTProbeError(f"{context}.path must be a non-empty string")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise PVTProbeError(f"{context}.path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PVTProbeError(f"{context}.path escapes its artifact root") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise PVTProbeError(f"{context} does not resolve to a regular file")
    return resolved


def resolve_by_digest(
    search_root: Path,
    manifest_root: Path,
    specification: Any,
    *,
    context: str,
) -> Path:
    if not isinstance(specification, dict):
        raise PVTProbeError(f"{context} artifact specification is malformed")
    digest = require_digest(specification.get("sha256"), context=f"{context}.sha256")
    raw_path = specification.get("path")
    candidates: list[Path] = []
    if isinstance(raw_path, str) and raw_path:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PVTProbeError(f"{context}.path is unsafe")
        for base in (manifest_root, search_root):
            candidate = (base / relative).resolve()
            try:
                candidate.relative_to(search_root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and not candidate.is_symlink():
                candidates.append(candidate)
    if isinstance(raw_path, str) and raw_path:
        basename = Path(raw_path).name
        candidates.extend(
            path
            for path in search_root.rglob(basename)
            if path.is_file() and not path.is_symlink()
        )
    unique: dict[Path, str] = {}
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique[resolved] = sha256_file(resolved)
    matches = [path for path, actual in unique.items() if actual == digest]
    if len(matches) != 1:
        raise PVTProbeError(
            f"{context} expected one digest match, found {len(matches)}"
        )
    return matches[0]


def validate_physical_evidence(
    physical_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    evidence_path = exactly_one(physical_root, "openroad_physical_evidence.json")
    evidence = load_json(evidence_path)
    if evidence.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise PVTProbeError("unsupported OpenROAD physical-evidence schema")
    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(BACKENDS):
        raise PVTProbeError("physical evidence does not cover the three backends")
    claims = evidence.get("claims")
    required_claims = (
        "registered_source_binding_verified",
        "pinned_orfs_image_used",
        "all_three_backends_placed",
        "all_three_backends_routed",
        "all_three_backends_emitted_spef",
        "two_attempts_per_backend_completed",
        "physical_repeatability_verified",
        "common_physical_boundary_verified",
    )
    if not isinstance(claims, dict) or any(
        claims.get(name) is not True for name in required_claims
    ):
        raise PVTProbeError("physical evidence lacks a required prerequisite")

    prepared_path = exactly_one(physical_root, "prepared.json")
    prepared = load_json(prepared_path)
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise PVTProbeError("unsupported prepared physical-evidence schema")
    prepared_backends = prepared.get("backends")
    if not isinstance(prepared_backends, dict) or set(prepared_backends) != set(
        BACKENDS
    ):
        raise PVTProbeError("prepared evidence does not cover the three backends")
    source = evidence.get("source")
    if not isinstance(source, dict):
        raise PVTProbeError("physical evidence source binding is malformed")
    expected_prepared = source.get("prepared_manifest_sha256")
    if expected_prepared is not None:
        expected = require_digest(
            expected_prepared,
            context="physical.source.prepared_manifest_sha256",
        )
        if sha256_file(prepared_path) != expected:
            raise PVTProbeError("physical evidence is bound to another prepared manifest")
    return evidence, prepared, evidence_path


def select_attempt_one_manifests(physical_root: Path) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for path in sorted(physical_root.rglob("openroad_run.json")):
        manifest = load_json(path)
        backend = manifest.get("backend")
        attempt = manifest.get("attempt")
        if backend in BACKENDS and attempt == 1:
            if backend in selected:
                raise PVTProbeError(f"multiple attempt-one manifests for {backend}")
            selected[str(backend)] = path
    if set(selected) != set(BACKENDS):
        raise PVTProbeError(
            f"attempt-one manifests differ from required backends: {sorted(selected)}"
        )
    return selected


def liberty_score(path: Path, label: str) -> int:
    name = path.name.lower().replace("-", "_")
    if "sg13g2" not in name or "stdcell" not in name or path.suffix != ".lib":
        return -1
    score = 0
    if label in name:
        score += 10
    if label == "typ":
        score += 5 if "1p20" in name or "1v20" in name else 0
        score += 5 if "25c" in name or "_25" in name else 0
    elif label == "slow":
        score += 5 if "1p08" in name or "1v08" in name else 0
        score += 5 if "125c" in name or "125" in name else 0
    elif label == "fast":
        score += 5 if "1p32" in name or "1v32" in name else 0
        score += 5 if "m40" in name or "minus40" in name else 0
    return score


def discover_liberty_corners(pdk_root: Path) -> dict[str, Path]:
    candidates = sorted(
        path
        for path in pdk_root.rglob("*.lib")
        if path.is_file() and not path.is_symlink()
    )
    if not candidates:
        raise PVTProbeError(f"no Liberty files found below {pdk_root}")
    selected: dict[str, Path] = {}
    for label in CORNER_LABELS:
        ranked = sorted(
            ((liberty_score(path, label), path) for path in candidates),
            key=lambda item: (-item[0], item[1].as_posix()),
        )
        if not ranked or ranked[0][0] < 15:
            raise PVTProbeError(f"no unambiguous {label} Liberty corner was found")
        best_score = ranked[0][0]
        tied = [path for score, path in ranked if score == best_score]
        if len(tied) != 1:
            raise PVTProbeError(
                f"{label} Liberty selection is ambiguous at score {best_score}: {tied}"
            )
        selected[label] = tied[0]
    if len({path.resolve() for path in selected.values()}) != len(CORNER_LABELS):
        raise PVTProbeError("corner discovery selected the same Liberty more than once")
    return selected


def git_head(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not SHA256_RE.fullmatch(value):
        raise PVTProbeError(f"cannot identify pinned PDK commit below {path}")
    return value


def opensta_version(opensta: Path) -> str:
    completed = subprocess.run(
        [str(opensta), "-version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not output:
        raise PVTProbeError(f"cannot identify OpenSTA executable {opensta}")
    return output.splitlines()[0]


def tighten_sdc(text: str, period_ns: float) -> str:
    if not math.isfinite(period_ns) or period_ns <= 0:
        raise ValueError("tight clock period must be finite and positive")
    replacement = rf"\g<prefix>{period_ns:.9g}"
    tightened, count = CLOCK_PERIOD_RE.subn(replacement, text, count=1)
    if count != 1:
        raise PVTProbeError("SDC does not contain exactly one replaceable create_clock")
    return tightened


def emit_opensta_script(
    *,
    liberty: Path,
    netlist: Path,
    top: str,
    sdc: Path,
    spef: Path,
    label: str,
) -> str:
    module = require_module(top, context="OpenSTA top module")
    values = {
        "liberty": liberty.resolve().as_posix(),
        "netlist": netlist.resolve().as_posix(),
        "sdc": sdc.resolve().as_posix(),
        "spef": spef.resolve().as_posix(),
    }
    if any("{" in value or "}" in value for value in values.values()):
        raise PVTProbeError("OpenSTA artifact paths may not contain braces")
    return "\n".join(
        [
            "sta::set_sta_continue_on_error 0",
            f"read_liberty {{{values['liberty']}}}",
            f"read_verilog {{{values['netlist']}}}",
            f"link_design {module}",
            f"read_sdc {{{values['sdc']}}}",
            f"read_spef {{{values['spef']}}}",
            "check_setup",
            "set_propagated_clock [all_clocks]",
            f'puts "HEPHAESTUS_PVT_CORNER={label}"',
            "report_checks -path_delay max -group_count 5 -endpoint_count 1",
            "report_worst_slack -max",
            "report_tns",
            'puts "HEPHAESTUS_PVT_DONE=1"',
            "exit",
            "",
        ]
    )


def parse_opensta_metrics(stdout: str) -> dict[str, Any]:
    slack_matches = SLACK_RE.findall(stdout)
    if not slack_matches:
        raise PVTProbeError("OpenSTA output does not contain a parseable setup slack")
    slack_value, status = slack_matches[-1]
    tns_matches = TNS_RE.findall(stdout)
    tns = float(tns_matches[-1]) if tns_matches else None
    slack = float(slack_value)
    if not math.isfinite(slack) or (tns is not None and not math.isfinite(tns)):
        raise PVTProbeError("OpenSTA returned a non-finite timing metric")
    if "HEPHAESTUS_PVT_DONE=1" not in stdout:
        raise PVTProbeError("OpenSTA script did not reach its completion marker")
    return {
        "worst_setup_slack_ns": slack,
        "slack_status": status.lower(),
        "total_negative_slack_ns": tns,
    }


def run_opensta(
    *,
    opensta: Path,
    workdir: Path,
    script: str,
    attempt: int,
    timeout: int,
) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    script_path = workdir / f"attempt-{attempt}.tcl"
    stdout_path = workdir / f"attempt-{attempt}.stdout.txt"
    stderr_path = workdir / f"attempt-{attempt}.stderr.txt"
    script_path.write_text(script, encoding="utf-8")
    try:
        completed = subprocess.run(
            [str(opensta.resolve()), str(script_path.resolve())],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or "", encoding="utf-8")
        raise PVTProbeError(f"OpenSTA attempt {attempt} timed out") from exc
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise PVTProbeError(
            f"OpenSTA attempt {attempt} failed with return code "
            f"{completed.returncode}"
        )
    metrics = parse_opensta_metrics(completed.stdout)
    return {
        "attempt": attempt,
        "returncode": completed.returncode,
        "metrics": metrics,
        "script": script_path.name,
        "script_sha256": sha256_file(script_path),
        "stdout": stdout_path.name,
        "stdout_sha256": sha256_file(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": sha256_file(stderr_path),
    }


def metrics_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("slack_status") != right.get("slack_status"):
        return False
    for name in ("worst_setup_slack_ns", "total_negative_slack_ns"):
        left_value = left.get(name)
        right_value = right.get(name)
        if left_value is None or right_value is None:
            if left_value != right_value:
                return False
            continue
        if not math.isclose(
            float(left_value),
            float(right_value),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            return False
    return True


def build_probe(
    physical_root: Path,
    pdk_root: Path,
    opensta: Path,
    output_dir: Path,
    *,
    expected_pdk_commit: str,
    timeout: int = 600,
    negative_period_ns: float = 0.05,
) -> dict[str, Any]:
    physical_root = physical_root.resolve()
    pdk_root = pdk_root.resolve()
    output_dir = output_dir.resolve()
    opensta = opensta.resolve()
    if not opensta.is_file() or opensta.is_symlink():
        raise PVTProbeError(f"OpenSTA executable is invalid: {opensta}")
    expected_commit = require_digest(
        expected_pdk_commit,
        context="expected PDK commit",
    )
    actual_commit = git_head(pdk_root)
    if actual_commit != expected_commit:
        raise PVTProbeError(
            f"PDK checkout differs: expected {expected_commit}, got {actual_commit}"
        )

    physical, prepared, physical_path = validate_physical_evidence(physical_root)
    run_manifests = select_attempt_one_manifests(physical_root)
    liberty = discover_liberty_corners(pdk_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_dir = output_dir / "source"
    source_dir.mkdir(exist_ok=True)
    shutil.copyfile(physical_path, source_dir / "openroad_physical_evidence.json")
    prepared_path = exactly_one(physical_root, "prepared.json")
    shutil.copyfile(prepared_path, source_dir / "prepared.json")

    evidence_backends: dict[str, Any] = {}
    for backend in BACKENDS:
        prepared_backend = prepared["backends"][backend]
        if not isinstance(prepared_backend, dict):
            raise PVTProbeError(f"prepared backend {backend} is malformed")
        top = require_module(
            prepared_backend.get("wrapper_module"),
            context=f"prepared backend {backend}.wrapper_module",
        )
        run_path = run_manifests[backend]
        run = load_json(run_path)
        artifacts = run.get("artifacts")
        if not isinstance(artifacts, dict):
            raise PVTProbeError(f"physical run artifacts are malformed for {backend}")
        netlist = resolve_by_digest(
            physical_root,
            run_path.parent,
            artifacts.get("final_verilog"),
            context=f"{backend}.final_verilog",
        )
        spef = resolve_by_digest(
            physical_root,
            run_path.parent,
            artifacts.get("final_spef"),
            context=f"{backend}.final_spef",
        )
        sdc_spec = artifacts.get("final_sdc")
        if not isinstance(sdc_spec, dict):
            fallback = prepared_backend.get("sdc")
            fallback_digest = prepared_backend.get("sdc_sha256")
            sdc_spec = {"path": fallback, "sha256": fallback_digest}
        sdc = resolve_by_digest(
            physical_root,
            run_path.parent,
            sdc_spec,
            context=f"{backend}.final_sdc",
        )

        backend_dir = output_dir / "backends" / backend
        backend_dir.mkdir(parents=True, exist_ok=True)
        backend_corners: dict[str, Any] = {}
        for label in CORNER_LABELS:
            corner_dir = backend_dir / label
            script = emit_opensta_script(
                liberty=liberty[label],
                netlist=netlist,
                top=top,
                sdc=sdc,
                spef=spef,
                label=label,
            )
            attempts = [
                run_opensta(
                    opensta=opensta,
                    workdir=corner_dir,
                    script=script,
                    attempt=attempt,
                    timeout=timeout,
                )
                for attempt in (1, 2)
            ]
            repeatable = metrics_equal(
                attempts[0]["metrics"],
                attempts[1]["metrics"],
            )
            if not repeatable:
                raise PVTProbeError(
                    f"OpenSTA metrics are not repeatable for {backend}/{label}"
                )
            backend_corners[label] = {
                "liberty": liberty[label].relative_to(pdk_root).as_posix(),
                "liberty_sha256": sha256_file(liberty[label]),
                "attempts": attempts,
                "repeatability_verified": True,
                "metrics": attempts[0]["metrics"],
            }

        negative_dir = backend_dir / "negative-control"
        negative_sdc = negative_dir / "tight.sdc"
        negative_dir.mkdir(parents=True, exist_ok=True)
        negative_sdc.write_text(
            tighten_sdc(sdc.read_text(encoding="utf-8"), negative_period_ns),
            encoding="utf-8",
        )
        negative_script = emit_opensta_script(
            liberty=liberty["typ"],
            netlist=netlist,
            top=top,
            sdc=negative_sdc,
            spef=spef,
            label="typ-tight-clock-negative-control",
        )
        negative = run_opensta(
            opensta=opensta,
            workdir=negative_dir,
            script=negative_script,
            attempt=1,
            timeout=timeout,
        )
        if negative["metrics"]["worst_setup_slack_ns"] >= 0:
            raise PVTProbeError(
                f"tight-clock negative control did not violate timing for {backend}"
            )
        evidence_backends[backend] = {
            "top_module": top,
            "physical_run_manifest": run_path.relative_to(physical_root).as_posix(),
            "physical_run_manifest_sha256": sha256_file(run_path),
            "routed_verilog": {
                "path": netlist.relative_to(physical_root).as_posix(),
                "sha256": sha256_file(netlist),
            },
            "routed_spef": {
                "path": spef.relative_to(physical_root).as_posix(),
                "sha256": sha256_file(spef),
            },
            "sdc": {
                "path": sdc.relative_to(physical_root).as_posix(),
                "sha256": sha256_file(sdc),
            },
            "corners": backend_corners,
            "negative_control": {
                "clock_period_ns": negative_period_ns,
                "sdc_sha256": sha256_file(negative_sdc),
                "analysis": negative,
                "timing_violation_observed": True,
            },
        }

    result = {
        "schema": "hephaestus.ihp-pvt-corner-research.v1",
        "evidence_level": "routed_spef_opensta_three_corner_research",
        "source": {
            "physical_evidence": physical_path.relative_to(physical_root).as_posix(),
            "physical_evidence_sha256": sha256_file(physical_path),
            "prepared_manifest_sha256": sha256_file(prepared_path),
        },
        "toolchain": {
            "opensta_version": opensta_version(opensta),
            "opensta_sha256": sha256_file(opensta),
            "ihp_open_pdk_commit": actual_commit,
        },
        "corner_order": list(CORNER_LABELS),
        "backends": evidence_backends,
        "claims": {
            "physical_evidence_prerequisite_verified": True,
            "routed_netlists_bound_by_digest": True,
            "routed_spef_bound_by_digest": True,
            "official_ihp_open_pdk_commit_pinned": True,
            "three_liberty_corners_bound_by_digest": True,
            "all_backend_corner_analyses_completed": True,
            "two_attempt_repeatability_verified": True,
            "tight_clock_negative_control_violated": True,
            "multi_corner_timing_observed": True,
            "comparative_pvt_claim_enabled": False,
            "ocv_aocv_pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_json(output_dir / "pvt_corner_research.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a digest-bound IHP routed multi-corner timing probe."
    )
    parser.add_argument("physical_root", type=Path)
    parser.add_argument("--pdk", required=True, type=Path)
    parser.add_argument("--opensta", required=True, type=Path)
    parser.add_argument("--expected-pdk-commit", required=True)
    parser.add_argument("--out", type=Path, default=Path("build/pvt-corner/evidence"))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--negative-period-ns", type=float, default=0.05)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_probe(
            args.physical_root,
            args.pdk,
            args.opensta,
            args.out,
            expected_pdk_commit=args.expected_pdk_commit,
            timeout=args.timeout,
            negative_period_ns=args.negative_period_ns,
        )
    except (PVTProbeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
