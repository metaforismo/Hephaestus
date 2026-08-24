"""Digest-bound routed multi-corner timing evidence for IHP SG13G2.

The module consumes same-head matched OpenROAD and post-physical equivalence
artifacts, binds exact routed Verilog/SDC/SPEF inputs, analyzes three official
IHP Open PDK Liberty corners twice with OpenSTA, and exercises a tight-clock
negative control for every backend.

The resulting evidence is deterministic three-corner characterization, not
foundry-signoff STA, OCV/AOCV/POCV, statistical variation, crosstalk analysis,
or silicon verification.
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

from .report import sha256_file, write_json

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_CORNER_LABELS = ("slow", "typ", "fast")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SLACK_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+slack\s+"
    r"\((MET|VIOLATED)\)\s*$",
    re.MULTILINE,
)
_TNS_RE = re.compile(
    r"^\s*tns\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CLOCK_PERIOD_RE = re.compile(
    r"(?P<prefix>\bcreate_clock\b[^\n]*?\s-period\s+)"
    r"(?P<period>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)


class PVTCornerError(RuntimeError):
    """Raised when PVT evidence cannot preserve its declared contract."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PVTCornerError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PVTCornerError(f"JSON root must be an object: {path}")
    return value


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PVTCornerError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _require_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise PVTCornerError(f"{context} is not a safe Verilog module name")
    return value


def _exactly_one(root: Path, name: str) -> Path:
    matches = sorted(root.rglob(name))
    if len(matches) != 1:
        raise PVTCornerError(
            f"expected exactly one {name!r} below {root}, found {len(matches)}"
        )
    path = matches[0]
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise PVTCornerError(f"invalid required artifact: {path}")
    return path


def _resolve_by_digest(
    search_root: Path,
    manifest_root: Path,
    specification: Any,
    *,
    context: str,
) -> Path:
    if not isinstance(specification, dict):
        raise PVTCornerError(f"{context} artifact specification is malformed")
    digest = _require_digest(
        specification.get("sha256"),
        context=f"{context}.sha256",
    )
    raw_path = specification.get("path")
    candidates: list[Path] = []
    if isinstance(raw_path, str) and raw_path:
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PVTCornerError(f"{context}.path is unsafe")
        for base in (manifest_root, search_root):
            candidate = (base / relative).resolve()
            try:
                candidate.relative_to(search_root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and not candidate.is_symlink():
                candidates.append(candidate)
        candidates.extend(
            path
            for path in search_root.rglob(relative.name)
            if path.is_file() and not path.is_symlink()
        )
    unique: dict[Path, str] = {}
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique[resolved] = sha256_file(resolved)
    matches = [path for path, actual in unique.items() if actual == digest]
    if len(matches) != 1:
        raise PVTCornerError(
            f"{context} expected one digest match, found {len(matches)}"
        )
    return matches[0]


def _validate_contract(path: Path) -> dict[str, Any]:
    contract = _load_json(path)
    if contract.get("schema") != "hephaestus.ihp-pvt-corner-contract.v1":
        raise PVTCornerError("unsupported PVT-corner contract schema")
    if contract.get("contract_id") != "ihp-sg13g2-routed-pvt-corner-v1":
        raise PVTCornerError("unexpected PVT-corner contract identity")
    if contract.get("backends") != list(_BACKENDS):
        raise PVTCornerError("PVT contract backend order differs")
    if contract.get("corner_order") != list(_CORNER_LABELS):
        raise PVTCornerError("PVT contract corner order differs")
    if contract.get("attempts_per_corner") != 2:
        raise PVTCornerError("PVT contract must require two attempts per corner")
    timeout = contract.get("timeout_seconds")
    if type(timeout) is not int or timeout <= 0:
        raise PVTCornerError("PVT contract timeout must be a positive integer")
    negative_period = contract.get("negative_control_clock_period_ns")
    if type(negative_period) not in (int, float) or not math.isfinite(
        float(negative_period)
    ):
        raise PVTCornerError("PVT negative-control period is invalid")
    if float(negative_period) <= 0:
        raise PVTCornerError("PVT negative-control period must be positive")
    pdk = contract.get("ihp_open_pdk")
    if not isinstance(pdk, dict):
        raise PVTCornerError("PVT contract PDK binding is malformed")
    _require_digest(pdk.get("commit"), context="contract.ihp_open_pdk.commit")
    selectors = contract.get("corner_selectors")
    if not isinstance(selectors, dict) or set(selectors) != set(_CORNER_LABELS):
        raise PVTCornerError("PVT contract corner selectors are malformed")
    for label, selector in selectors.items():
        if not isinstance(selector, dict):
            raise PVTCornerError(f"corner selector {label} is malformed")
        required = selector.get("required_filename_tokens")
        if (
            not isinstance(required, list)
            or not required
            or any(not isinstance(token, str) or not token for token in required)
        ):
            raise PVTCornerError(f"corner selector {label} tokens are malformed")
    claims = contract.get("claim_boundary")
    if not isinstance(claims, dict):
        raise PVTCornerError("PVT contract claim boundary is malformed")
    required_false = (
        "ocv_aocv_pocv_analyzed",
        "statistical_variation_analyzed",
        "crosstalk_delay_analyzed",
        "foundry_signoff_sta_performed",
        "foundry_signoff_complete",
        "silicon_verified",
    )
    if any(claims.get(name) is not False for name in required_false):
        raise PVTCornerError("PVT contract overstates its claim boundary")
    return contract


def _validate_physical_evidence(
    physical_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
    evidence_path = _exactly_one(
        physical_root,
        "openroad_physical_evidence.json",
    )
    evidence = _load_json(evidence_path)
    if evidence.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise PVTCornerError("unsupported OpenROAD physical-evidence schema")
    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise PVTCornerError("physical evidence does not cover three backends")
    claims = evidence.get("claims")
    required = (
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
        claims.get(name) is not True for name in required
    ):
        raise PVTCornerError("physical evidence lacks a required prerequisite")

    prepared_path = _exactly_one(physical_root, "prepared.json")
    prepared = _load_json(prepared_path)
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise PVTCornerError("unsupported prepared physical-evidence schema")
    prepared_backends = prepared.get("backends")
    if not isinstance(prepared_backends, dict) or set(prepared_backends) != set(
        _BACKENDS
    ):
        raise PVTCornerError("prepared evidence does not cover three backends")
    source = evidence.get("source")
    if not isinstance(source, dict):
        raise PVTCornerError("physical evidence source binding is malformed")
    expected = source.get("prepared_manifest_sha256")
    if expected is not None:
        digest = _require_digest(
            expected,
            context="physical.source.prepared_manifest_sha256",
        )
        if sha256_file(prepared_path) != digest:
            raise PVTCornerError("physical evidence binds another prepared manifest")
    return evidence, prepared, evidence_path, prepared_path


def _validate_post_physical_evidence(
    post_physical_root: Path,
    physical_evidence_path: Path,
) -> tuple[dict[str, Any], Path]:
    path = _exactly_one(
        post_physical_root,
        "post_physical_equivalence_evidence.json",
    )
    evidence = _load_json(path)
    if evidence.get("schema") != (
        "hephaestus.post-physical-equivalence-evidence.v1"
    ):
        raise PVTCornerError("unsupported post-physical evidence schema")
    claims = evidence.get("claims")
    required = (
        "clock_edge_post_physical_equivalence_verified",
        "post_physical_equivalence_verified",
        "comparative_ppa_claim_enabled",
    )
    if not isinstance(claims, dict) or any(
        claims.get(name) is not True for name in required
    ):
        raise PVTCornerError("post-physical equivalence prerequisite is incomplete")
    source = evidence.get("source")
    if not isinstance(source, dict):
        raise PVTCornerError("post-physical source binding is malformed")
    expected_physical = source.get("physical_evidence_sha256")
    if expected_physical is not None:
        expected = _require_digest(
            expected_physical,
            context="post_physical.source.physical_evidence_sha256",
        )
        if sha256_file(physical_evidence_path) != expected:
            raise PVTCornerError(
                "post-physical evidence binds another physical evidence manifest"
            )
    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise PVTCornerError("post-physical evidence does not cover three backends")
    return evidence, path


def _select_attempt_one_manifests(physical_root: Path) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for path in sorted(physical_root.rglob("openroad_run.json")):
        manifest = _load_json(path)
        backend = manifest.get("backend")
        attempt = manifest.get("attempt")
        if backend in _BACKENDS and attempt == 1:
            name = str(backend)
            if name in selected:
                raise PVTCornerError(f"multiple attempt-one manifests for {name}")
            selected[name] = path
    if set(selected) != set(_BACKENDS):
        raise PVTCornerError(
            f"attempt-one manifests differ from required backends: {sorted(selected)}"
        )
    return selected


def _discover_liberty_corners(
    pdk_root: Path,
    contract: dict[str, Any],
) -> dict[str, Path]:
    files = sorted(
        path
        for path in pdk_root.rglob("*.lib")
        if path.is_file() and not path.is_symlink()
    )
    if not files:
        raise PVTCornerError(f"no Liberty files found below {pdk_root}")
    selectors = contract["corner_selectors"]
    selected: dict[str, Path] = {}
    for label in _CORNER_LABELS:
        required = [
            token.lower()
            for token in selectors[label]["required_filename_tokens"]
        ]
        matches = [
            path
            for path in files
            if all(token in path.name.lower() for token in required)
        ]
        if len(matches) != 1:
            raise PVTCornerError(
                f"corner {label} expected one Liberty match, found {len(matches)}"
            )
        selected[label] = matches[0]
    if len({path.resolve() for path in selected.values()}) != len(_CORNER_LABELS):
        raise PVTCornerError("corner selectors chose the same Liberty more than once")
    return selected


def _git_head(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or _SHA256_RE.fullmatch(value) is None:
        raise PVTCornerError(f"cannot identify PDK commit below {path}")
    return value


def _opensta_version(executable: Path) -> str:
    completed = subprocess.run(
        [str(executable), "-version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not output:
        raise PVTCornerError(f"cannot identify OpenSTA executable {executable}")
    return output.splitlines()[0]


def _tighten_sdc(text: str, period_ns: float) -> str:
    if not math.isfinite(period_ns) or period_ns <= 0:
        raise ValueError("tight clock period must be finite and positive")
    replacement = rf"\g<prefix>{period_ns:.9g}"
    value, count = _CLOCK_PERIOD_RE.subn(replacement, text, count=1)
    if count != 1:
        raise PVTCornerError("SDC lacks one replaceable create_clock period")
    return value


def _emit_opensta_script(
    *,
    liberty: Path,
    netlist: Path,
    top: str,
    sdc: Path,
    spef: Path,
    label: str,
) -> str:
    module = _require_module(top, context="OpenSTA top module")
    paths = {
        "liberty": liberty.resolve().as_posix(),
        "netlist": netlist.resolve().as_posix(),
        "sdc": sdc.resolve().as_posix(),
        "spef": spef.resolve().as_posix(),
    }
    if any("{" in value or "}" in value for value in paths.values()):
        raise PVTCornerError("OpenSTA artifact paths may not contain braces")
    return "\n".join(
        [
            "sta::set_sta_continue_on_error 0",
            f"read_liberty {{{paths['liberty']}}}",
            f"read_verilog {{{paths['netlist']}}}",
            f"link_design {module}",
            f"read_sdc {{{paths['sdc']}}}",
            f"read_spef {{{paths['spef']}}}",
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


def _parse_metrics(stdout: str) -> dict[str, Any]:
    slack_matches = _SLACK_RE.findall(stdout)
    if not slack_matches:
        raise PVTCornerError("OpenSTA output lacks a parseable setup slack")
    slack_raw, status = slack_matches[-1]
    tns_matches = _TNS_RE.findall(stdout)
    slack = float(slack_raw)
    tns = float(tns_matches[-1]) if tns_matches else None
    if not math.isfinite(slack) or (tns is not None and not math.isfinite(tns)):
        raise PVTCornerError("OpenSTA returned a non-finite timing metric")
    if "HEPHAESTUS_PVT_DONE=1" not in stdout:
        raise PVTCornerError("OpenSTA script did not reach its completion marker")
    return {
        "worst_setup_slack_ns": slack,
        "slack_status": status.lower(),
        "total_negative_slack_ns": tns,
    }


def _run_opensta(
    *,
    executable: Path,
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
            [str(executable.resolve()), str(script_path.resolve())],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        stdout_path.write_text(stdout or "", encoding="utf-8")
        stderr_path.write_text(stderr or "", encoding="utf-8")
        raise PVTCornerError(f"OpenSTA attempt {attempt} timed out") from exc
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise PVTCornerError(
            f"OpenSTA attempt {attempt} failed with return code "
            f"{completed.returncode}"
        )
    metrics = _parse_metrics(completed.stdout)
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


def _metrics_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
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


def run_evidence(
    physical_root: Path,
    post_physical_root: Path,
    pdk_root: Path,
    opensta: Path,
    contract_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Run and bind the permanent three-corner timing evidence."""

    physical_root = physical_root.resolve()
    post_physical_root = post_physical_root.resolve()
    pdk_root = pdk_root.resolve()
    opensta = opensta.resolve()
    output_dir = output_dir.resolve()
    if not opensta.is_file() or opensta.is_symlink():
        raise PVTCornerError(f"OpenSTA executable is invalid: {opensta}")

    contract = _validate_contract(contract_path)
    physical, prepared, physical_path, prepared_path = (
        _validate_physical_evidence(physical_root)
    )
    post_physical, post_physical_path = _validate_post_physical_evidence(
        post_physical_root,
        physical_path,
    )
    run_manifests = _select_attempt_one_manifests(physical_root)

    actual_commit = _git_head(pdk_root)
    expected_commit = contract["ihp_open_pdk"]["commit"]
    if actual_commit != expected_commit:
        raise PVTCornerError(
            f"PDK checkout differs: expected {expected_commit}, got {actual_commit}"
        )
    liberties = _discover_liberty_corners(pdk_root, contract)
    timeout = int(contract["timeout_seconds"])
    attempts_per_corner = int(contract["attempts_per_corner"])
    negative_period = float(contract["negative_control_clock_period_ns"])
    output_dir.mkdir(parents=True, exist_ok=True)

    source_dir = output_dir / "source"
    source_dir.mkdir(exist_ok=True)
    source_files = {
        "openroad_physical_evidence.json": physical_path,
        "prepared.json": prepared_path,
        "post_physical_equivalence_evidence.json": post_physical_path,
        "pvt_contract.json": contract_path,
    }
    for name, source in source_files.items():
        shutil.copyfile(source, source_dir / name)

    evidence_backends: dict[str, Any] = {}
    for backend in _BACKENDS:
        prepared_backend = prepared["backends"][backend]
        if not isinstance(prepared_backend, dict):
            raise PVTCornerError(f"prepared backend {backend} is malformed")
        top = _require_module(
            prepared_backend.get("wrapper_module"),
            context=f"prepared backend {backend}.wrapper_module",
        )
        run_path = run_manifests[backend]
        run = _load_json(run_path)
        artifacts = run.get("artifacts")
        if not isinstance(artifacts, dict):
            raise PVTCornerError(f"physical run artifacts are malformed for {backend}")
        netlist = _resolve_by_digest(
            physical_root,
            run_path.parent,
            artifacts.get("final_verilog"),
            context=f"{backend}.final_verilog",
        )
        spef = _resolve_by_digest(
            physical_root,
            run_path.parent,
            artifacts.get("final_spef"),
            context=f"{backend}.final_spef",
        )
        sdc_spec = artifacts.get("final_sdc")
        if not isinstance(sdc_spec, dict):
            sdc_spec = {
                "path": prepared_backend.get("sdc"),
                "sha256": prepared_backend.get("sdc_sha256"),
            }
        sdc = _resolve_by_digest(
            physical_root,
            run_path.parent,
            sdc_spec,
            context=f"{backend}.final_sdc",
        )

        backend_dir = output_dir / "backends" / backend
        backend_dir.mkdir(parents=True, exist_ok=True)
        corner_evidence: dict[str, Any] = {}
        for label in _CORNER_LABELS:
            corner_dir = backend_dir / label
            script = _emit_opensta_script(
                liberty=liberties[label],
                netlist=netlist,
                top=top,
                sdc=sdc,
                spef=spef,
                label=label,
            )
            attempts = [
                _run_opensta(
                    executable=opensta,
                    workdir=corner_dir,
                    script=script,
                    attempt=attempt,
                    timeout=timeout,
                )
                for attempt in range(1, attempts_per_corner + 1)
            ]
            if any(
                not _metrics_equal(attempts[0]["metrics"], item["metrics"])
                for item in attempts[1:]
            ):
                raise PVTCornerError(
                    f"OpenSTA metrics are not repeatable for {backend}/{label}"
                )
            corner_evidence[label] = {
                "liberty": liberties[label].relative_to(pdk_root).as_posix(),
                "liberty_sha256": sha256_file(liberties[label]),
                "attempts": attempts,
                "repeatability_verified": True,
                "metrics": attempts[0]["metrics"],
            }

        negative_dir = backend_dir / "negative-control"
        negative_dir.mkdir(parents=True, exist_ok=True)
        negative_sdc = negative_dir / "tight.sdc"
        negative_sdc.write_text(
            _tighten_sdc(sdc.read_text(encoding="utf-8"), negative_period),
            encoding="utf-8",
        )
        negative_script = _emit_opensta_script(
            liberty=liberties["typ"],
            netlist=netlist,
            top=top,
            sdc=negative_sdc,
            spef=spef,
            label="typ-tight-clock-negative-control",
        )
        negative = _run_opensta(
            executable=opensta,
            workdir=negative_dir,
            script=negative_script,
            attempt=1,
            timeout=timeout,
        )
        if negative["metrics"]["worst_setup_slack_ns"] >= 0:
            raise PVTCornerError(
                f"tight-clock negative control did not violate timing for {backend}"
            )

        post_backend = post_physical["backends"][backend]
        if not isinstance(post_backend, dict):
            raise PVTCornerError(f"post-physical backend {backend} is malformed")
        evidence_backends[backend] = {
            "top_module": top,
            "post_physical_backend": post_backend,
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
            "corners": corner_evidence,
            "negative_control": {
                "clock_period_ns": negative_period,
                "sdc_sha256": sha256_file(negative_sdc),
                "analysis": negative,
                "timing_violation_observed": True,
            },
        }

    claims = {
        "physical_evidence_prerequisite_verified": True,
        "post_physical_equivalence_prerequisite_verified": True,
        "routed_netlists_bound_by_digest": True,
        "routed_spef_bound_by_digest": True,
        "official_ihp_open_pdk_commit_pinned": True,
        "three_liberty_corners_bound_by_digest": True,
        "all_backend_corner_analyses_completed": True,
        "two_attempt_repeatability_verified": True,
        "tight_clock_negative_control_violated": True,
        "multi_corner_timing_observed": True,
        "comparative_pvt_claim_enabled": True,
        **contract["claim_boundary"],
    }
    evidence = {
        "schema": "hephaestus.ihp-pvt-corner-evidence.v1",
        "evidence_level": "routed_spef_opensta_three_corner_characterization",
        "contract": {
            "path": contract_path.as_posix(),
            "sha256": sha256_file(contract_path),
            "value": contract,
        },
        "source": {
            "physical_evidence_sha256": sha256_file(physical_path),
            "prepared_manifest_sha256": sha256_file(prepared_path),
            "post_physical_equivalence_sha256": sha256_file(post_physical_path),
        },
        "toolchain": {
            "opensta_version": _opensta_version(opensta),
            "opensta_sha256": sha256_file(opensta),
            "ihp_open_pdk_commit": actual_commit,
        },
        "corner_order": list(_CORNER_LABELS),
        "backends": evidence_backends,
        "claims": claims,
    }
    write_json(output_dir / "pvt_corner_evidence.json", evidence)
    return evidence


def build_reference(evidence_path: Path, output_path: Path) -> dict[str, Any]:
    """Build a compact regression reference from a qualified evidence manifest."""

    evidence = _load_json(evidence_path)
    if evidence.get("schema") not in {
        "hephaestus.ihp-pvt-corner-research.v1",
        "hephaestus.ihp-pvt-corner-evidence.v1",
    }:
        raise PVTCornerError("unsupported PVT evidence schema for reference")
    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise PVTCornerError("PVT evidence reference lacks three backends")
    reference_backends: dict[str, Any] = {}
    for backend in _BACKENDS:
        value = backends[backend]
        corners = value.get("corners")
        if not isinstance(corners, dict) or set(corners) != set(_CORNER_LABELS):
            raise PVTCornerError(f"PVT backend {backend} corners are malformed")
        reference_backends[backend] = {
            "top_module": value["top_module"],
            "routed_verilog_sha256": value["routed_verilog"]["sha256"],
            "routed_spef_sha256": value["routed_spef"]["sha256"],
            "sdc_sha256": value["sdc"]["sha256"],
            "corners": {
                label: {
                    "liberty_sha256": corners[label]["liberty_sha256"],
                    "metrics": corners[label]["metrics"],
                }
                for label in _CORNER_LABELS
            },
            "negative_control": {
                "clock_period_ns": value["negative_control"]["clock_period_ns"],
                "timing_violation_observed": value["negative_control"][
                    "timing_violation_observed"
                ],
            },
        }
    reference = {
        "schema": "hephaestus.ihp-pvt-corner-reference.v1",
        "reference_id": "ihp-sg13g2-routed-pvt-corner-tiny-v1",
        "source_schema": evidence["schema"],
        "ihp_open_pdk_commit": evidence["toolchain"]["ihp_open_pdk_commit"],
        "corner_order": list(_CORNER_LABELS),
        "backends": reference_backends,
        "claim_boundary": {
            "comparative_pvt_claim_enabled": True,
            "ocv_aocv_pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_json(output_path, reference)
    return reference


def validate_reference(
    evidence_path: Path,
    reference_path: Path,
) -> dict[str, Any]:
    """Validate stable PVT observations against the versioned reference."""

    evidence = _load_json(evidence_path)
    reference = _load_json(reference_path)
    if evidence.get("schema") != "hephaestus.ihp-pvt-corner-evidence.v1":
        raise PVTCornerError("reference validation requires permanent PVT evidence")
    if reference.get("schema") != "hephaestus.ihp-pvt-corner-reference.v1":
        raise PVTCornerError("unsupported PVT reference schema")
    if reference.get("reference_id") != "ihp-sg13g2-routed-pvt-corner-tiny-v1":
        raise PVTCornerError("unexpected PVT reference identity")
    if evidence["toolchain"]["ihp_open_pdk_commit"] != reference.get(
        "ihp_open_pdk_commit"
    ):
        raise PVTCornerError("PVT evidence uses another PDK commit")
    if evidence.get("corner_order") != reference.get("corner_order"):
        raise PVTCornerError("PVT evidence corner order changed")
    for backend in _BACKENDS:
        actual = evidence["backends"][backend]
        expected = reference["backends"][backend]
        stable = {
            "top_module": actual["top_module"],
            "routed_verilog_sha256": actual["routed_verilog"]["sha256"],
            "routed_spef_sha256": actual["routed_spef"]["sha256"],
            "sdc_sha256": actual["sdc"]["sha256"],
            "corners": {
                label: {
                    "liberty_sha256": actual["corners"][label]["liberty_sha256"],
                    "metrics": actual["corners"][label]["metrics"],
                }
                for label in _CORNER_LABELS
            },
            "negative_control": {
                "clock_period_ns": actual["negative_control"]["clock_period_ns"],
                "timing_violation_observed": actual["negative_control"][
                    "timing_violation_observed"
                ],
            },
        }
        if stable != expected:
            raise PVTCornerError(f"PVT regression changed for backend {backend}")
    if reference.get("claim_boundary") != {
        name: evidence["claims"].get(name)
        for name in reference["claim_boundary"]
    }:
        raise PVTCornerError("PVT evidence claim boundary differs from reference")
    result = {
        "schema": "hephaestus.ihp-pvt-corner-reference-validation.v1",
        "evidence_sha256": sha256_file(evidence_path),
        "reference_sha256": sha256_file(reference_path),
        "passed": True,
    }
    return result


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    return run_evidence(
        args.physical_root,
        args.post_physical_root,
        args.pdk,
        args.opensta,
        args.contract,
        args.out,
    )


def _reference_command(args: argparse.Namespace) -> dict[str, Any]:
    return build_reference(args.evidence, args.out)


def _validate_command(args: argparse.Namespace) -> dict[str, Any]:
    result = validate_reference(args.evidence, args.reference)
    if args.out is not None:
        write_json(args.out, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or validate digest-bound IHP PVT timing evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("physical_root", type=Path)
    run_parser.add_argument("post_physical_root", type=Path)
    run_parser.add_argument("--pdk", required=True, type=Path)
    run_parser.add_argument("--opensta", required=True, type=Path)
    run_parser.add_argument("--contract", required=True, type=Path)
    run_parser.add_argument("--out", required=True, type=Path)
    run_parser.set_defaults(handler=_run_command)

    reference_parser = subparsers.add_parser("reference")
    reference_parser.add_argument("evidence", type=Path)
    reference_parser.add_argument("--out", required=True, type=Path)
    reference_parser.set_defaults(handler=_reference_command)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("evidence", type=Path)
    validate_parser.add_argument("--reference", required=True, type=Path)
    validate_parser.add_argument("--out", type=Path)
    validate_parser.set_defaults(handler=_validate_command)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = args.handler(args)
    except (PVTCornerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
