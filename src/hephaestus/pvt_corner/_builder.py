"""Permanent exact-head routed IHP PVT evidence builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import (
    ANALYSIS_REPLAYS,
    BACKENDS,
    CORNERS,
    EVIDENCE_LEVEL,
    PHYSICAL_ATTEMPTS,
    SCHEMA,
    PVTCornerError,
    copy_bound,
    execution_context,
    load_json,
    paths_overlap,
    resolve_input_directory,
    resolve_input_file,
    resolve_output_directory,
    sha256_file,
    write_json,
)
from ._opensta import (
    emit_opensta_script,
    metrics_equal,
    replay_run,
    run_opensta,
    tighten_sdc,
)
from ._reference import load_reference, make_reference, validate_reference
from ._source import validate_source_chain


def _copy_source_chain(chain: dict[str, Any], output: Path) -> dict[str, Any]:
    source = output / "source"
    source.mkdir(parents=True)
    copied: dict[str, Any] = {}
    for name, path in (
        ("openroad_physical_evidence.json", chain["physical_path"]),
        ("prepared.json", chain["prepared_path"]),
        ("post_physical_equivalence_evidence.json", chain["post_physical_path"]),
        ("pvt_contract.json", chain["contract_path"]),
        ("opensta_tool.json", chain["opensta"]["manifest_path"]),
    ):
        digest = sha256_file(path)
        destination = source / name
        copy_bound(path, destination, expected_digest=digest, context=name)
        copied[name] = {
            "path": destination.relative_to(output).as_posix(),
            "sha256": digest,
        }

    liberty_dir = source / "liberty"
    liberty_dir.mkdir()
    liberty: dict[str, Any] = {}
    for label in CORNERS:
        value = chain["pdk"]["liberties"][label]
        destination = liberty_dir / f"{label}.lib"
        copy_bound(
            value["path"],
            destination,
            expected_digest=value["sha256"],
            context=f"{label} Liberty",
        )
        liberty[label] = {
            "path": destination.relative_to(output).as_posix(),
            "sha256": value["sha256"],
            "git_blob_sha": value["git_blob_sha"],
            "pdk_relative_path": value["relative_path"],
            "nominal_voltage_v": value["nominal_voltage_v"],
            "nominal_temperature_c": value["nominal_temperature_c"],
        }
    copied["liberty"] = liberty
    return copied


def _stage_case(
    case: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    backend = case["backend"]
    attempt = case["physical_attempt"]
    directory = output / "source" / "cases" / backend / f"physical-attempt-{attempt}"
    directory.mkdir(parents=True)
    staged: dict[str, Any] = {}
    for label, source, filename, digest_key in (
        (
            "run_manifest",
            case["run_manifest"],
            "openroad_run.json",
            "run_manifest_sha256",
        ),
        (
            "routed_verilog",
            case["routed_verilog"],
            "routed.v",
            "routed_verilog_sha256",
        ),
        (
            "routed_spef",
            case["routed_spef"],
            "routed.spef",
            "routed_spef_sha256",
        ),
        ("sdc", case["sdc"], "constraint.sdc", "sdc_sha256"),
    ):
        destination = directory / filename
        expected = case[digest_key]
        copy_bound(source, destination, expected_digest=expected, context=f"{backend} {label}")
        staged[label] = {
            "path": destination.relative_to(output).as_posix(),
            "sha256": expected,
        }
    staged.update(
        {
            "backend": backend,
            "physical_attempt": attempt,
            "top_module": case["top_module"],
            "spef_date_normalized_sha256": case["spef_date_normalized_sha256"],
            "directory": directory,
        }
    )
    return staged


def _positive_analysis(
    *,
    output: Path,
    executable: Path,
    staged: dict[str, Any],
    liberty: dict[str, Any],
    corner: str,
    replay: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    backend = staged["backend"]
    attempt = staged["physical_attempt"]
    workdir = (
        output
        / "backends"
        / backend
        / f"physical-attempt-{attempt}"
        / "corners"
        / corner
        / f"replay-{replay}"
    )
    script = emit_opensta_script(
        liberty=output / liberty["path"],
        netlist=output / staged["routed_verilog"]["path"],
        top_module=staged["top_module"],
        sdc=output / staged["sdc"]["path"],
        spef=output / staged["routed_spef"]["path"],
        corner_label=corner,
    )
    record = run_opensta(
        executable=executable,
        workdir=workdir,
        script=script,
        label=corner,
        replay=replay,
        timeout_seconds=timeout_seconds,
    )
    replayed = replay_run(workdir, record, expected_label=corner)
    if not metrics_equal(replayed, record["metrics"]):
        raise PVTCornerError("positive OpenSTA report replay changed its metrics")
    return record


def _negative_control(
    *,
    output: Path,
    executable: Path,
    staged: dict[str, Any],
    typ_liberty: dict[str, Any],
    baseline_metrics: dict[str, Any],
    clock_period_ns: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    backend = staged["backend"]
    attempt = staged["physical_attempt"]
    workdir = output / "backends" / backend / f"physical-attempt-{attempt}" / "negative-control"
    negative_sdc = (
        output / "source" / "cases" / backend / f"physical-attempt-{attempt}" / "tight-clock.sdc"
    )
    source_sdc = output / staged["sdc"]["path"]
    negative_sdc.write_text(
        tighten_sdc(source_sdc.read_text(encoding="utf-8"), clock_period_ns),
        encoding="utf-8",
    )
    label = "typ-tight-clock-control"
    script = emit_opensta_script(
        liberty=output / typ_liberty["path"],
        netlist=output / staged["routed_verilog"]["path"],
        top_module=staged["top_module"],
        sdc=negative_sdc,
        spef=output / staged["routed_spef"]["path"],
        corner_label=label,
    )
    record = run_opensta(
        executable=executable,
        workdir=workdir,
        script=script,
        label=label,
        replay=1,
        timeout_seconds=timeout_seconds,
    )
    metrics = replay_run(workdir, record, expected_label=label)
    slack = float(metrics["worst_setup_slack_ns"])
    baseline_slack = float(baseline_metrics["worst_setup_slack_ns"])
    tns = float(metrics["total_negative_slack_ns"])
    if slack >= 0 or tns >= 0:
        raise PVTCornerError(
            f"tight-clock control did not violate setup timing for {backend}/{attempt}"
        )
    if slack >= baseline_slack:
        raise PVTCornerError(
            f"tight-clock control did not worsen setup slack for {backend}/{attempt}"
        )
    return {
        "clock_period_ns": clock_period_ns,
        "sdc": negative_sdc.relative_to(output).as_posix(),
        "sdc_sha256": sha256_file(negative_sdc),
        "analysis": record,
        "timing_violation_observed": True,
        "baseline_typ_slack_ns": baseline_slack,
        "control_slack_ns": slack,
    }


def _collect_evidence(
    physical_root: Path,
    post_physical_root: Path,
    pdk_root: Path,
    opensta_executable: Path,
    opensta_manifest_path: Path,
    contract_path: Path,
    output: Path,
    *,
    source_revision: str,
    upstream_run_id: str | None = None,
) -> dict[str, Any]:
    chain = validate_source_chain(
        physical_root,
        post_physical_root,
        pdk_root,
        opensta_executable,
        opensta_manifest_path,
        contract_path,
        source_revision=source_revision,
    )
    output.mkdir(parents=True)
    copied = _copy_source_chain(chain, output)
    staged_cases: dict[str, dict[int, Any]] = {}
    for backend in BACKENDS:
        staged_cases[backend] = {}
        for attempt in PHYSICAL_ATTEMPTS:
            staged_cases[backend][attempt] = _stage_case(
                chain["cases"][backend][attempt],
                output,
            )

    contract = chain["contract"]
    timeout = int(contract["timeout_seconds"])
    negative_period = float(contract["negative_control_clock_period_ns"])
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_level": EVIDENCE_LEVEL,
        "execution": execution_context(
            source_revision,
            upstream_run_id=upstream_run_id,
        ),
        "source": copied,
        "contract": {
            "path": copied["pvt_contract.json"]["path"],
            "sha256": copied["pvt_contract.json"]["sha256"],
            "value": contract,
        },
        "toolchain": {
            "ihp_open_pdk_commit": chain["pdk"]["commit"],
            "opensta_commit": chain["opensta"]["commit"],
            "opensta_banner": chain["opensta"]["banner"],
            "opensta_binary_sha256": chain["opensta"]["binary_sha256"],
            "opensta_tool_manifest_sha256": chain["opensta"]["manifest_sha256"],
            "liberty": copied["liberty"],
        },
        "corner_order": list(CORNERS),
        "backends": {},
    }

    for backend in BACKENDS:
        backend_result: dict[str, Any] = {
            "top_module": staged_cases[backend][1]["top_module"],
            "physical_attempts": {},
        }
        for attempt in PHYSICAL_ATTEMPTS:
            staged = staged_cases[backend][attempt]
            case_result: dict[str, Any] = {
                "run_manifest_sha256": staged["run_manifest"]["sha256"],
                "routed_verilog_sha256": staged["routed_verilog"]["sha256"],
                "routed_spef_sha256": staged["routed_spef"]["sha256"],
                "spef_date_normalized_sha256": staged["spef_date_normalized_sha256"],
                "sdc_sha256": staged["sdc"]["sha256"],
                "corners": {},
            }
            for corner in CORNERS:
                replays = [
                    _positive_analysis(
                        output=output,
                        executable=chain["opensta"]["executable"],
                        staged=staged,
                        liberty=copied["liberty"][corner],
                        corner=corner,
                        replay=replay,
                        timeout_seconds=timeout,
                    )
                    for replay in ANALYSIS_REPLAYS
                ]
                if not metrics_equal(replays[0]["metrics"], replays[1]["metrics"]):
                    raise PVTCornerError(
                        f"OpenSTA analysis is not replayable for "
                        f"{backend}/attempt-{attempt}/{corner}"
                    )
                case_result["corners"][corner] = {
                    "liberty_sha256": copied["liberty"][corner]["sha256"],
                    "replays": replays,
                    "metrics": replays[0]["metrics"],
                    "analysis_replay_repeatability_verified": True,
                    "raw_report_replay_verified": True,
                }
            case_result["negative_control"] = _negative_control(
                output=output,
                executable=chain["opensta"]["executable"],
                staged=staged,
                typ_liberty=copied["liberty"]["typ"],
                baseline_metrics=case_result["corners"]["typ"]["metrics"],
                clock_period_ns=negative_period,
                timeout_seconds=timeout,
            )
            backend_result["physical_attempts"][str(attempt)] = case_result

        first = backend_result["physical_attempts"]["1"]
        second = backend_result["physical_attempts"]["2"]
        for corner in CORNERS:
            if not metrics_equal(
                first["corners"][corner]["metrics"],
                second["corners"][corner]["metrics"],
            ):
                raise PVTCornerError(f"physical-attempt timing differs for {backend}/{corner}")
        backend_result["physical_attempt_timing_repeatability_verified"] = True
        evidence["backends"][backend] = backend_result

    evidence["claims"] = {
        "physical_evidence_prerequisite_verified": True,
        "post_physical_equivalence_prerequisite_verified": True,
        "all_six_routed_timing_cases_bound": True,
        "official_ihp_open_pdk_commit_pinned": True,
        "three_liberty_corners_bound_by_sha256": True,
        "all_36_positive_analyses_completed": True,
        "analysis_replay_repeatability_verified": True,
        "physical_attempt_timing_repeatability_verified": True,
        "six_tight_clock_negative_controls_detected": True,
        "raw_report_replay_verified": True,
        "multi_corner_timing_observed": True,
        "comparative_pvt_claim_enabled": False,
        **contract["claim_boundary"],
    }
    return evidence


def build_evidence(
    physical_root: str | Path,
    post_physical_root: str | Path,
    pdk_root: str | Path,
    opensta_executable: str | Path,
    opensta_manifest_path: str | Path,
    contract_path: str | Path,
    output_dir: str | Path,
    *,
    source_revision: str,
    reference_path: str | Path | None = None,
    upstream_run_id: str | None = None,
) -> dict[str, Any]:
    """Run the complete routed PVT matrix and optionally qualify it by reference."""

    physical = resolve_input_directory(
        Path(physical_root),
        context="physical evidence artifact",
    )
    post = resolve_input_directory(
        Path(post_physical_root),
        context="post-physical evidence artifact",
    )
    pdk = resolve_input_directory(Path(pdk_root), context="IHP Open PDK checkout")
    opensta = resolve_input_file(
        Path(opensta_executable),
        context="OpenSTA executable",
    )
    tool_manifest = resolve_input_file(
        Path(opensta_manifest_path),
        context="OpenSTA tool manifest",
    )
    contract = resolve_input_file(Path(contract_path), context="PVT contract")
    reference = (
        resolve_input_file(Path(reference_path), context="PVT regression reference")
        if reference_path is not None
        else None
    )
    output = resolve_output_directory(Path(output_dir))
    for label, protected in (
        ("physical artifact", physical),
        ("post-physical artifact", post),
        ("PDK checkout", pdk),
        ("OpenSTA executable", opensta),
        ("OpenSTA tool manifest", tool_manifest),
        ("PVT contract", contract),
    ):
        if paths_overlap(output, protected):
            raise PVTCornerError(f"output directory overlaps the {label}: {output} and {protected}")
    if reference is not None and paths_overlap(output, reference):
        raise PVTCornerError("output directory overlaps the PVT reference")

    evidence = _collect_evidence(
        physical,
        post,
        pdk,
        opensta,
        tool_manifest,
        contract,
        output,
        source_revision=source_revision,
        upstream_run_id=upstream_run_id,
    )
    if reference is None:
        evidence["regression"] = {
            "passed": False,
            "bootstrap_reference_required": True,
        }
    else:
        reference_value = load_reference(reference)
        reference_digest = sha256_file(reference)
        copy_bound(
            reference,
            output / "source" / "pvt_reference.json",
            expected_digest=reference_digest,
            context="PVT regression reference",
        )
        evidence["source"]["pvt_reference.json"] = {
            "path": "source/pvt_reference.json",
            "sha256": reference_digest,
        }
        evidence["regression"] = validate_reference(
            evidence,
            reference_value,
            reference_sha256=reference_digest,
        )
        evidence["claims"]["comparative_pvt_claim_enabled"] = True

    write_json(output / "pvt_corner_evidence.json", evidence)
    lines = [
        "# Routed IHP SG13G2 PVT evidence",
        "",
        f"- source revision: `{source_revision}`",
        "- routed cases: `6`",
        "- Liberty corners: `3`",
        "- positive OpenSTA analyses: `36`",
        "- tight-clock negative controls: `6`",
        f"- regression passed: `{str(evidence['regression']['passed']).lower()}`",
        "- OCV/AOCV/POCV, crosstalk, sign-off, and silicon: `false`",
        "",
    ]
    (output / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    return evidence


def build_reference(evidence_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Create a versioned reference from an inspected bootstrap evidence artifact."""

    evidence_file = resolve_input_file(Path(evidence_path), context="PVT evidence")
    evidence = load_json(evidence_file)
    if evidence.get("schema") != SCHEMA:
        raise PVTCornerError("unsupported PVT evidence schema for reference creation")
    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        raise PVTCornerError("PVT evidence claims are malformed")
    required = (
        "all_36_positive_analyses_completed",
        "analysis_replay_repeatability_verified",
        "physical_attempt_timing_repeatability_verified",
        "six_tight_clock_negative_controls_detected",
        "raw_report_replay_verified",
        "multi_corner_timing_observed",
    )
    if any(claims.get(name) is not True for name in required):
        raise PVTCornerError("PVT evidence is not qualified enough to seed a reference")
    output = Path(output_path)
    if output.exists():
        raise PVTCornerError(f"reference output already exists: {output}")
    reference = make_reference(evidence)
    write_json(output, reference)
    return reference


def validate_existing_reference(
    evidence_path: str | Path,
    reference_path: str | Path,
) -> dict[str, Any]:
    evidence_file = resolve_input_file(Path(evidence_path), context="PVT evidence")
    reference_file = resolve_input_file(Path(reference_path), context="PVT reference")
    evidence = load_json(evidence_file)
    reference = load_reference(reference_file)
    return validate_reference(
        evidence,
        reference,
        reference_sha256=sha256_file(reference_file),
    )


__all__ = [
    "build_evidence",
    "build_reference",
    "validate_existing_reference",
    "_collect_evidence",
]
