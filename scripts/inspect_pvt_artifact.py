#!/usr/bin/env python3
"""Independently replay and inspect a routed PVT evidence artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from hephaestus.pvt_corner._builder import validate_existing_reference
from hephaestus.pvt_corner._common import BACKENDS, CORNERS, PHYSICAL_ATTEMPTS
from hephaestus.pvt_corner._opensta import metrics_equal, replay_run

_DIAGNOSTIC_RE = re.compile(r"(?mi)^\s*(?:Warning:|%Warning|Error:|%Error|FATAL:).*$")
_EXPECTED_PDK_COMMIT = "22f2a25f1734796de3debbbf29cf697cbbc54081"
_EXPECTED_OPENSTA_COMMIT = "2b751f0e8196b05ef4ed8246b7e27c63c967ec6d"
_EXPECTED_LIBERTY = {
    "slow": "1ac6a0301184b3a8aa1a4a01910967f624c8e869517777f529985f11f04588c3",
    "typ": "7677a8918689f452e80405ad16a83e744709342574f2aedcc507c2758986b396",
    "fast": "f191a3132d0f2f59ca2721884cdb1d8360c8fbf3e0012056af62583dd69ae701",
}


class InspectionError(RuntimeError):
    """Raised when the artifact does not satisfy the independent contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InspectionError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InspectionError(f"JSON artifact must be an object: {path}")
    return value


def _safe_root(path: Path) -> Path:
    raw = Path(os.path.abspath(os.fspath(path)))
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        if current.is_symlink():
            raise InspectionError(f"artifact root must not contain symlinks: {raw}")
    root = raw.resolve()
    if not root.is_dir():
        raise InspectionError(f"artifact root is not a directory: {root}")
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise InspectionError(f"artifact contains a symlink: {candidate}")
    return root


def _resolve(root: Path, relative: Any, *, context: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise InspectionError(f"{context} path must be a non-empty string")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise InspectionError(f"{context} path is unsafe: {relative!r}")
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise InspectionError(f"{context} path escapes the artifact root") from exc
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise InspectionError(f"{context} is not a non-empty regular file: {path}")
    return path


def _check_source_bindings(root: Path, value: object) -> int:
    checked = 0
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            expected = value["sha256"]
            if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                raise InspectionError("source binding has an invalid SHA-256 digest")
            path = _resolve(root, value["path"], context="source binding")
            actual = _sha256(path)
            if actual != expected:
                raise InspectionError(
                    f"source binding digest differs for {path}: expected {expected}, got {actual}"
                )
            checked += 1
        for child in value.values():
            checked += _check_source_bindings(root, child)
    elif isinstance(value, list):
        for child in value:
            checked += _check_source_bindings(root, child)
    return checked


def _diagnostics(stdout: Path, stderr: Path) -> list[str]:
    text = stdout.read_text(encoding="utf-8") + "\n" + stderr.read_text(encoding="utf-8")
    return [match.group(0).strip() for match in _DIAGNOSTIC_RE.finditer(text)]


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise InspectionError("artifact tree is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = bytes.fromhex(_sha256(path))
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(payload)
    return digest.hexdigest()


def _require_false_claims(claims: dict[str, Any]) -> None:
    for name in (
        "ocv_analyzed",
        "aocv_analyzed",
        "pocv_analyzed",
        "statistical_variation_analyzed",
        "crosstalk_delay_analyzed",
        "ir_drop_analyzed",
        "electromigration_analyzed",
        "thermal_analyzed",
        "foundry_signoff_sta_performed",
        "foundry_signoff_complete",
        "silicon_verified",
    ):
        if claims.get(name) is not False:
            raise InspectionError(f"claim must remain false: {name}")


def inspect_artifact(
    artifact_root: str | Path,
    *,
    expected_source_revision: str,
    strict: bool,
) -> dict[str, Any]:
    """Replay all reports, provenance bindings, controls, and the reference."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_source_revision) is None:
        raise InspectionError("expected source revision must be a 40-character Git SHA")
    root = _safe_root(Path(artifact_root))
    evidence_files = list(root.rglob("pvt_corner_evidence.json"))
    if len(evidence_files) != 1:
        raise InspectionError(
            f"expected exactly one PVT evidence manifest, found {len(evidence_files)}"
        )
    evidence_path = evidence_files[0]
    evidence_root = evidence_path.parent
    evidence = _load_object(evidence_path)

    if evidence.get("schema") != "hephaestus.ihp-pvt-corner-evidence.v2":
        raise InspectionError("unsupported PVT evidence schema")
    if evidence.get("evidence_level") != "routed_spef_opensta_three_corner_characterization":
        raise InspectionError("unexpected PVT evidence level")
    execution = evidence.get("execution")
    if not isinstance(execution, dict):
        raise InspectionError("PVT execution provenance is malformed")
    if execution.get("source_revision") != expected_source_revision:
        raise InspectionError("PVT evidence was generated from another source revision")
    upstream = execution.get("upstream_physical_workflow_run_id")
    if not isinstance(upstream, str) or not upstream.isdigit() or int(upstream) <= 0:
        raise InspectionError("PVT evidence lacks a valid upstream physical workflow run ID")

    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        raise InspectionError("PVT claims are malformed")
    for name in (
        "physical_evidence_prerequisite_verified",
        "post_physical_equivalence_prerequisite_verified",
        "all_six_routed_timing_cases_bound",
        "official_ihp_open_pdk_commit_pinned",
        "three_liberty_corners_bound_by_sha256",
        "all_36_positive_analyses_completed",
        "analysis_replay_repeatability_verified",
        "physical_attempt_timing_repeatability_verified",
        "six_tight_clock_negative_controls_detected",
        "raw_report_replay_verified",
        "multi_corner_timing_observed",
    ):
        if claims.get(name) is not True:
            raise InspectionError(f"required PVT claim is not true: {name}")
    _require_false_claims(claims)
    if claims.get("comparative_pvt_claim_enabled") is not strict:
        raise InspectionError("comparative PVT claim does not match inspection mode")

    toolchain = evidence.get("toolchain")
    if not isinstance(toolchain, dict):
        raise InspectionError("PVT toolchain is malformed")
    if toolchain.get("ihp_open_pdk_commit") != _EXPECTED_PDK_COMMIT:
        raise InspectionError("IHP Open PDK commit differs")
    if toolchain.get("opensta_commit") != _EXPECTED_OPENSTA_COMMIT:
        raise InspectionError("OpenSTA commit differs")
    liberty = toolchain.get("liberty")
    if (
        not isinstance(liberty, dict)
        or {label: liberty.get(label, {}).get("sha256") for label in CORNERS} != _EXPECTED_LIBERTY
    ):
        raise InspectionError("PVT Liberty SHA-256 matrix differs")

    source = evidence.get("source")
    if not isinstance(source, dict):
        raise InspectionError("PVT source bindings are malformed")
    source_bindings = _check_source_bindings(evidence_root, source)

    positive = 0
    controls = 0
    diagnostic_count = 0
    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(BACKENDS):
        raise InspectionError("PVT backend set differs")
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for backend in BACKENDS:
        backend_value = backends[backend]
        if backend_value.get("physical_attempt_timing_repeatability_verified") is not True:
            raise InspectionError(f"physical-attempt repeatability is false for {backend}")
        attempts = backend_value.get("physical_attempts")
        if not isinstance(attempts, dict) or set(attempts) != {"1", "2"}:
            raise InspectionError(f"physical attempt set differs for {backend}")
        metrics[backend] = {}
        for attempt in PHYSICAL_ATTEMPTS:
            case = attempts[str(attempt)]
            corners = case.get("corners")
            if not isinstance(corners, dict) or set(corners) != set(CORNERS):
                raise InspectionError(f"corner set differs for {backend}/attempt-{attempt}")
            metrics[backend][str(attempt)] = {}
            for corner in CORNERS:
                corner_value = corners[corner]
                if corner_value.get("liberty_sha256") != _EXPECTED_LIBERTY[corner]:
                    raise InspectionError(f"Liberty binding differs for {backend}/{corner}")
                if corner_value.get("analysis_replay_repeatability_verified") is not True:
                    raise InspectionError(f"analysis replay flag is false for {backend}/{corner}")
                if corner_value.get("raw_report_replay_verified") is not True:
                    raise InspectionError(f"raw replay flag is false for {backend}/{corner}")
                replays = corner_value.get("replays")
                if not isinstance(replays, list) or len(replays) != 2:
                    raise InspectionError(f"replay count differs for {backend}/{corner}")
                replayed = []
                for replay_index, record in enumerate(replays, start=1):
                    workdir = (
                        evidence_root
                        / "backends"
                        / backend
                        / f"physical-attempt-{attempt}"
                        / "corners"
                        / corner
                        / f"replay-{replay_index}"
                    )
                    replayed.append(replay_run(workdir, record, expected_label=corner))
                    warnings = _diagnostics(workdir / "stdout.txt", workdir / "stderr.txt")
                    if warnings:
                        raise InspectionError(
                            f"OpenSTA diagnostics for {backend}/attempt-{attempt}/{corner}: "
                            + " | ".join(warnings[:5])
                        )
                    diagnostic_count += len(warnings)
                    positive += 1
                if not metrics_equal(replayed[0], replayed[1]):
                    raise InspectionError(f"replay metrics differ for {backend}/{corner}")
                if not metrics_equal(replayed[0], corner_value.get("metrics", {})):
                    raise InspectionError(f"recorded metrics differ for {backend}/{corner}")
                for numeric in ("worst_setup_slack_ns", "total_negative_slack_ns"):
                    value = replayed[0].get(numeric)
                    if type(value) not in (int, float) or not math.isfinite(float(value)):
                        raise InspectionError(
                            f"non-finite PVT metric: {backend}/{corner}/{numeric}"
                        )
                metrics[backend][str(attempt)][corner] = replayed[0]

            control = case.get("negative_control")
            if (
                not isinstance(control, dict)
                or control.get("timing_violation_observed") is not True
            ):
                raise InspectionError(f"negative control is malformed for {backend}/{attempt}")
            workdir = (
                evidence_root
                / "backends"
                / backend
                / f"physical-attempt-{attempt}"
                / "negative-control"
            )
            control_metrics = replay_run(
                workdir,
                control["analysis"],
                expected_label="typ-tight-clock-control",
            )
            warnings = _diagnostics(workdir / "stdout.txt", workdir / "stderr.txt")
            if warnings:
                raise InspectionError(
                    f"OpenSTA control diagnostics for {backend}/attempt-{attempt}: "
                    + " | ".join(warnings[:5])
                )
            diagnostic_count += len(warnings)
            if float(control_metrics["worst_setup_slack_ns"]) >= 0:
                raise InspectionError(f"control slack is not negative for {backend}/{attempt}")
            if float(control_metrics["total_negative_slack_ns"]) >= 0:
                raise InspectionError(f"control TNS is not negative for {backend}/{attempt}")
            if not float(control["control_slack_ns"]) < float(control["baseline_typ_slack_ns"]):
                raise InspectionError(f"control does not worsen timing for {backend}/{attempt}")
            controls += 1

        for corner in CORNERS:
            if not metrics_equal(metrics[backend]["1"][corner], metrics[backend]["2"][corner]):
                raise InspectionError(f"physical-attempt metrics differ for {backend}/{corner}")

    if positive != 36 or controls != 6:
        raise InspectionError(
            f"PVT matrix is incomplete: positives={positive}, controls={controls}"
        )

    regression = evidence.get("regression")
    if not isinstance(regression, dict):
        raise InspectionError("PVT regression result is malformed")
    reference_result: dict[str, Any] | None
    if strict:
        if regression.get("passed") is not True:
            raise InspectionError("strict PVT regression did not pass")
        reference_record = source.get("pvt_reference.json")
        if not isinstance(reference_record, dict):
            raise InspectionError("strict PVT evidence lacks a copied reference")
        reference = _resolve(evidence_root, reference_record["path"], context="PVT reference")
        reference_result = validate_existing_reference(evidence_path, reference)
        if reference_result.get("passed") is not True:
            raise InspectionError("independent PVT reference validation failed")
    else:
        if regression != {"bootstrap_reference_required": True, "passed": False}:
            raise InspectionError("bootstrap PVT regression boundary differs")
        reference_result = None

    return {
        "schema": "hephaestus.pvt-artifact-independent-inspection.v1",
        "source_revision": expected_source_revision,
        "strict_reference_mode": strict,
        "positive_analyses_replayed": positive,
        "negative_controls_replayed": controls,
        "source_sha256_bindings_rechecked": source_bindings,
        "opensta_diagnostics_observed": diagnostic_count,
        "reference_validation": reference_result,
        "artifact_tree_sha256": _tree_sha256(evidence_root),
        "result": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = inspect_artifact(
        args.artifact_root,
        expected_source_revision=args.expected_source_revision,
        strict=args.strict,
    )
    if args.out.exists():
        raise InspectionError(f"inspection output already exists: {args.out}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
