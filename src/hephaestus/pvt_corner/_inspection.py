"""Independent replay and provenance inspection for routed PVT artifacts."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ._common import (
    ANALYSIS_REPLAYS,
    BACKENDS,
    CORNERS,
    PHYSICAL_ATTEMPTS,
    PVTCornerError,
    load_json,
    require_git_sha,
    require_sha256,
    require_string,
    resolve_input_file,
    sha256_file,
    verify_file,
)
from ._opensta import metrics_equal, replay_run
from ._reference import (
    _validate_evidence_identity,
    _validate_runtime_claims,
)
from ._provenance import validate_upstream_run_binding

_REQUIRED_SOURCE_FILES = (
    "openroad_physical_evidence.json",
    "prepared.json",
    "post_physical_equivalence_evidence.json",
    "pvt_contract.json",
    "opensta_tool.json",
)
_CASE_FILES = {
    "run_manifest_sha256": "openroad_run.json",
    "routed_verilog_sha256": "routed.v",
    "routed_spef_sha256": "routed.spef",
    "sdc_sha256": "constraint.sdc",
}


def _require_mapping(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PVTCornerError(f"{context} is malformed")
    return value


def _verify_recorded_file(
    root: Path,
    specification: Any,
    *,
    expected_path: str,
    context: str,
) -> Path:
    spec = _require_mapping(specification, context=context)
    if spec.get("path") != expected_path:
        raise PVTCornerError(
            f"{context} path differs: expected {expected_path!r}, "
            f"got {spec.get('path')!r}"
        )
    return verify_file(
        root,
        spec.get("path"),
        spec.get("sha256"),
        context=context,
    )


def _verify_source_bundle(
    root: Path,
    evidence: dict[str, Any],
    *,
    comparative_enabled: bool,
) -> dict[str, Path]:
    source = _require_mapping(evidence.get("source"), context="PVT source bundle")
    expected_keys = set(_REQUIRED_SOURCE_FILES) | {"liberty"}
    if comparative_enabled:
        expected_keys.add("pvt_reference.json")
    if set(source) != expected_keys:
        raise PVTCornerError(
            "PVT source bundle has unexpected contents: "
            f"expected={sorted(expected_keys)}, got={sorted(source)}"
        )

    files: dict[str, Path] = {}
    for name in _REQUIRED_SOURCE_FILES:
        files[name] = _verify_recorded_file(
            root,
            source.get(name),
            expected_path=f"source/{name}",
            context=f"PVT source {name}",
        )
    if comparative_enabled:
        files["pvt_reference.json"] = _verify_recorded_file(
            root,
            source.get("pvt_reference.json"),
            expected_path="source/pvt_reference.json",
            context="PVT source regression reference",
        )

    liberty = _require_mapping(source.get("liberty"), context="PVT Liberty bundle")
    if set(liberty) != set(CORNERS):
        raise PVTCornerError("PVT Liberty source bundle has the wrong corner set")
    for label in CORNERS:
        files[f"liberty:{label}"] = _verify_recorded_file(
            root,
            liberty.get(label),
            expected_path=f"source/liberty/{label}.lib",
            context=f"PVT {label} Liberty source",
        )

    contract = _require_mapping(evidence.get("contract"), context="PVT contract record")
    contract_source = source["pvt_contract.json"]
    if (
        contract.get("path") != contract_source["path"]
        or contract.get("sha256") != contract_source["sha256"]
        or contract.get("value") != load_json(files["pvt_contract.json"])
    ):
        raise PVTCornerError("PVT contract record differs from its bound source file")

    toolchain = _require_mapping(evidence.get("toolchain"), context="PVT toolchain")
    tool_manifest = load_json(files["opensta_tool.json"])
    if tool_manifest.get("schema") != "hephaestus.opensta-tool.v1":
        raise PVTCornerError("unsupported OpenSTA tool manifest schema")
    if (
        require_git_sha(
            toolchain.get("opensta_commit"),
            context="PVT OpenSTA commit",
        )
        != require_git_sha(
            tool_manifest.get("commit"),
            context="OpenSTA manifest commit",
        )
        or require_sha256(
            toolchain.get("opensta_binary_sha256"),
            context="PVT OpenSTA binary",
        )
        != require_sha256(
            tool_manifest.get("binary_sha256"),
            context="OpenSTA manifest binary",
        )
        or toolchain.get("opensta_banner") != tool_manifest.get("banner")
        or toolchain.get("opensta_tool_manifest_sha256")
        != sha256_file(files["opensta_tool.json"])
    ):
        raise PVTCornerError("PVT OpenSTA toolchain differs from its bound manifest")

    tool_liberty = _require_mapping(
        toolchain.get("liberty"),
        context="PVT toolchain Liberty bundle",
    )
    if set(tool_liberty) != set(CORNERS):
        raise PVTCornerError("PVT toolchain Liberty corner set differs")
    for label in CORNERS:
        source_spec = liberty[label]
        tool_spec = _require_mapping(
            tool_liberty[label],
            context=f"PVT {label} toolchain Liberty",
        )
        for key in (
            "path",
            "sha256",
            "git_blob_sha",
            "pdk_relative_path",
            "nominal_voltage_v",
            "nominal_temperature_c",
        ):
            if tool_spec.get(key) != source_spec.get(key):
                raise PVTCornerError(
                    f"PVT {label} Liberty toolchain field {key} differs"
                )
    return files


def _verify_execution_provenance(
    root: Path,
    evidence: dict[str, Any],
    source_files: dict[str, Path],
) -> None:
    execution = _require_mapping(evidence.get("execution"), context="PVT execution")
    source_revision = require_git_sha(
        execution.get("source_revision"),
        context="PVT source revision",
    )
    post = load_json(source_files["post_physical_equivalence_evidence.json"])
    post_execution = _require_mapping(
        post.get("execution"),
        context="post-physical execution provenance",
    )
    if require_git_sha(
        post_execution.get("source_revision"),
        context="post-physical source revision",
    ) != source_revision:
        raise PVTCornerError("PVT source revision differs from post-physical evidence")

    upstream = execution.get("upstream_physical_workflow_run_id")
    if upstream is not None:
        # Reuse the public checker against a temporary artifact-shaped view.
        post_root = root / "source"
        expected = post_root / "post_physical_equivalence_evidence.json"
        if expected != source_files["post_physical_equivalence_evidence.json"]:
            raise PVTCornerError("PVT post-physical source path differs")
        validate_upstream_run_binding(post_root, require_string(upstream, context="PVT upstream run ID"))


def _verify_metric_coverage(metrics: Any, *, context: str) -> dict[str, Any]:
    value = _require_mapping(metrics, context=context)
    if value.get("check_setup_passed") is not True:
        raise PVTCornerError(f"{context} lacks a successful setup check")
    for key in ("clock_count", "timing_path_count"):
        count = value.get(key)
        if type(count) is not int or count < 1:
            raise PVTCornerError(f"{context} has an invalid {key}")
    for key in (
        "unannotated_driver_count",
        "partially_unannotated_driver_count",
    ):
        if value.get(key) != 0:
            raise PVTCornerError(f"{context} has incomplete SPEF annotation")
    return value


def _verify_analysis_matrix(root: Path, evidence: dict[str, Any]) -> tuple[int, int]:
    backends = _require_mapping(evidence.get("backends"), context="PVT backends")
    if set(backends) != set(BACKENDS):
        raise PVTCornerError("PVT artifact has the wrong backend set")

    positive_count = 0
    control_count = 0
    for backend in BACKENDS:
        backend_value = _require_mapping(
            backends[backend],
            context=f"PVT backend {backend}",
        )
        require_string(
            backend_value.get("top_module"),
            context=f"PVT {backend} top module",
        )
        attempts = _require_mapping(
            backend_value.get("physical_attempts"),
            context=f"PVT {backend} physical attempts",
        )
        expected_attempts = {str(value) for value in PHYSICAL_ATTEMPTS}
        if set(attempts) != expected_attempts:
            raise PVTCornerError(f"PVT {backend} physical attempt set differs")

        for attempt in PHYSICAL_ATTEMPTS:
            case = _require_mapping(
                attempts[str(attempt)],
                context=f"PVT {backend} physical attempt {attempt}",
            )
            case_root = (
                root
                / "source"
                / "cases"
                / backend
                / f"physical-attempt-{attempt}"
            )
            for digest_key, filename in _CASE_FILES.items():
                verify_file(
                    case_root,
                    filename,
                    case.get(digest_key),
                    context=f"PVT {backend}/{attempt} {filename}",
                )

            corners = _require_mapping(
                case.get("corners"),
                context=f"PVT {backend}/{attempt} corners",
            )
            if set(corners) != set(CORNERS):
                raise PVTCornerError(f"PVT {backend}/{attempt} corner set differs")
            for corner in CORNERS:
                corner_value = _require_mapping(
                    corners[corner],
                    context=f"PVT {backend}/{attempt}/{corner}",
                )
                if (
                    corner_value.get("analysis_replay_repeatability_verified")
                    is not True
                    or corner_value.get("raw_report_replay_verified") is not True
                ):
                    raise PVTCornerError(
                        f"PVT {backend}/{attempt}/{corner} replay predicates are false"
                    )
                recorded_metrics = _verify_metric_coverage(
                    corner_value.get("metrics"),
                    context=f"PVT {backend}/{attempt}/{corner} metrics",
                )
                replays = corner_value.get("replays")
                if not isinstance(replays, list) or len(replays) != len(
                    ANALYSIS_REPLAYS
                ):
                    raise PVTCornerError(
                        f"PVT {backend}/{attempt}/{corner} replay set differs"
                    )
                for replay, record in zip(ANALYSIS_REPLAYS, replays, strict=True):
                    record_value = _require_mapping(
                        record,
                        context=f"PVT {backend}/{attempt}/{corner} replay {replay}",
                    )
                    if record_value.get("replay") != replay:
                        raise PVTCornerError("PVT replay identity differs")
                    workdir = (
                        root
                        / "backends"
                        / backend
                        / f"physical-attempt-{attempt}"
                        / "corners"
                        / corner
                        / f"replay-{replay}"
                    )
                    replayed = replay_run(
                        workdir,
                        record_value,
                        expected_label=corner,
                    )
                    if not metrics_equal(replayed, recorded_metrics):
                        raise PVTCornerError(
                            f"PVT {backend}/{attempt}/{corner} replay metrics differ"
                        )
                    positive_count += 1

            control = _require_mapping(
                case.get("negative_control"),
                context=f"PVT {backend}/{attempt} negative control",
            )
            if control.get("timing_violation_observed") is not True:
                raise PVTCornerError("PVT tight-clock control predicate is false")
            tight_sdc = verify_file(
                case_root,
                "tight-clock.sdc",
                control.get("sdc_sha256"),
                context=f"PVT {backend}/{attempt} tight-clock SDC",
            )
            if control.get("sdc") != tight_sdc.relative_to(root).as_posix():
                raise PVTCornerError("PVT tight-clock SDC path differs")
            record = _require_mapping(
                control.get("analysis"),
                context=f"PVT {backend}/{attempt} control analysis",
            )
            workdir = (
                root
                / "backends"
                / backend
                / f"physical-attempt-{attempt}"
                / "negative-control"
            )
            replayed = replay_run(
                workdir,
                record,
                expected_label="typ-tight-clock-control",
            )
            _verify_metric_coverage(
                replayed,
                context=f"PVT {backend}/{attempt} control metrics",
            )
            slack = float(replayed["worst_setup_slack_ns"])
            tns = float(replayed["total_negative_slack_ns"])
            baseline = float(control.get("baseline_typ_slack_ns"))
            recorded_control = float(control.get("control_slack_ns"))
            if (
                not math.isfinite(baseline)
                or not math.isclose(slack, recorded_control, rel_tol=0.0, abs_tol=1e-9)
                or slack >= 0
                or tns >= 0
                or slack >= baseline
            ):
                raise PVTCornerError(
                    f"PVT {backend}/{attempt} tight-clock behavior differs"
                )
            control_count += 1

        first = attempts["1"]
        second = attempts["2"]
        for corner in CORNERS:
            if not metrics_equal(
                first["corners"][corner]["metrics"],
                second["corners"][corner]["metrics"],
            ):
                raise PVTCornerError(
                    f"PVT {backend}/{corner} physical-attempt metrics differ"
                )
        if backend_value.get("physical_attempt_timing_repeatability_verified") is not True:
            raise PVTCornerError(
                f"PVT {backend} physical-attempt repeatability predicate is false"
            )
    return positive_count, control_count


def inspect_evidence_artifact(
    evidence_path: str | Path,
    *,
    allowed_comparative_values: tuple[bool, ...] = (False, True),
    require_bootstrap: bool = False,
) -> dict[str, Any]:
    """Replay a complete PVT artifact before reference creation or validation."""

    evidence_file = resolve_input_file(Path(evidence_path), context="PVT evidence")
    evidence = load_json(evidence_file)
    _validate_evidence_identity(evidence)
    comparative = _validate_runtime_claims(
        evidence,
        allowed_comparative_values=allowed_comparative_values,
    )
    regression = evidence.get("regression")
    if require_bootstrap:
        if comparative is not False or regression != {
            "passed": False,
            "bootstrap_reference_required": True,
        }:
            raise PVTCornerError(
                "PVT reference creation requires the exact bootstrap state"
            )
    elif comparative is True:
        if not isinstance(regression, dict) or regression.get("passed") is not True:
            raise PVTCornerError("qualified PVT evidence lacks a passing regression")

    root = evidence_file.parent
    source_files = _verify_source_bundle(
        root,
        evidence,
        comparative_enabled=comparative,
    )
    _verify_execution_provenance(root, evidence, source_files)
    positives, controls = _verify_analysis_matrix(root, evidence)
    if positives != 36 or controls != 6:
        raise PVTCornerError(
            f"PVT artifact matrix differs: positives={positives}, controls={controls}"
        )
    return {
        "schema": "hephaestus.ihp-pvt-corner-artifact-inspection.v1",
        "evidence_sha256": sha256_file(evidence_file),
        "source_revision": evidence["execution"]["source_revision"],
        "comparative_pvt_claim_enabled": comparative,
        "positive_analyses_replayed": positives,
        "negative_controls_replayed": controls,
        "passed": True,
    }
