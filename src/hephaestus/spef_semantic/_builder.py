"""Permanent same-run routed SPEF semantic evidence builder."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._common import (
    _BACKENDS,
    _EVIDENCE_LEVEL,
    _FAULTS,
    _SCHEMA,
    SPEFSemanticError,
    _copy_bound,
    _execution_context,
    _load_json,
    _paths_overlap,
    _require_claims,
    _require_digest,
    _require_revision,
    _resolve_input_directory,
    _resolve_input_file,
    _resolve_output_directory,
    _resolve_under,
    _sha256,
    _sha256_bytes,
    _verify_file,
    _write_json,
)
from ._parser import parse_spef, parse_spef_text, parser_contract
from ._reference import _load_reference, make_reference, validate_reference

_DATE_RE = re.compile(r'(?m)^\*DATE\s+"[^"\r\n]*"\r?$')


def _date_normalized_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    normalized, count = _DATE_RE.subn('*DATE "<normalized>"', text)
    if count != 1:
        raise SPEFSemanticError(f"SPEF must contain exactly one *DATE record: {path}")
    return _sha256_bytes(normalized.encode("utf-8"))


def _validate_source_chain(
    physical_root: Path,
    post_physical_root: Path,
    *,
    source_revision: str | None,
) -> dict[str, Any]:
    physical_path = _resolve_under(
        physical_root,
        "evidence/openroad_physical_evidence.json",
        context="physical evidence manifest",
    )
    post_path = _resolve_under(
        post_physical_root,
        "post_physical_equivalence_evidence.json",
        context="post-physical equivalence manifest",
    )
    post_source_copy = _resolve_under(
        post_physical_root,
        "source/openroad_physical_evidence.json",
        context="post-physical physical-evidence copy",
    )
    physical = _load_json(physical_path)
    post = _load_json(post_path)

    if physical.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise SPEFSemanticError("unsupported physical evidence schema")
    if physical.get("evidence_level") != "matched_registered_orfs_rtl_to_gds_repeatability":
        raise SPEFSemanticError("unexpected physical evidence level")
    if post.get("schema") != "hephaestus.post-physical-equivalence-evidence.v1":
        raise SPEFSemanticError("unsupported post-physical evidence schema")
    if post.get("evidence_level") != "exact_registered_source_to_routed_sequential_equivalence":
        raise SPEFSemanticError("unexpected post-physical evidence level")

    _require_claims(
        physical.get("claims"),
        required_true=(
            "registered_source_binding_verified",
            "pinned_orfs_image_used",
            "all_three_backends_placed",
            "all_three_backends_routed",
            "all_three_backends_emitted_spef",
            "two_attempts_per_backend_completed",
            "physical_repeatability_verified",
            "physical_metrics_recorded",
            "common_physical_boundary_verified",
        ),
        required_false=(
            "post_physical_equivalence_verified",
            "comparative_ppa_claim_enabled",
            "drc_clean",
            "lvs_clean",
            "power_estimated_with_activity",
            "post_layout_pex_verified",
            "foundry_signoff_complete",
            "silicon_verified",
        ),
        context="physical evidence",
    )
    _require_claims(
        post.get("claims"),
        required_true=(
            "registered_source_binding_verified",
            "both_physical_attempts_per_backend_bound",
            "all_three_routed_registered_implementations_equivalent",
            "data_corruption_negative_control_detected",
            "valid_latency_negative_control_detected",
            "reset_state_negative_control_detected",
            "post_physical_equivalence_verified",
            "comparative_ppa_claim_enabled",
        ),
        required_false=(
            "four_state_semantics_verified",
            "timing_annotated_functional_semantics_verified",
            "drc_clean",
            "lvs_clean",
            "power_estimated_with_activity",
            "post_layout_pex_verified",
            "foundry_signoff_complete",
            "silicon_verified",
        ),
        context="post-physical evidence",
    )
    if post.get("regression", {}).get("passed") is not True:
        raise SPEFSemanticError("post-physical regression prerequisite did not pass")

    physical_digest = _sha256(physical_path)
    expected_physical = _require_digest(
        post.get("source", {}).get("physical_evidence_sha256"),
        context="post-physical physical evidence",
    )
    if physical_digest != expected_physical:
        raise SPEFSemanticError("post-physical evidence binds another physical manifest")
    if _sha256(post_source_copy) != physical_digest:
        raise SPEFSemanticError("post-physical physical-manifest copy differs")

    actual_revision = _require_revision(
        post.get("execution", {}).get("source_revision"),
        context="post-physical source revision",
    )
    if source_revision is not None and actual_revision != source_revision:
        raise SPEFSemanticError(
            "post-physical evidence was produced from another source revision: "
            f"expected {source_revision}, got {actual_revision}"
        )

    physical_backends = physical.get("backends")
    post_backends = post.get("backends")
    if not isinstance(physical_backends, dict) or set(physical_backends) != set(_BACKENDS):
        raise SPEFSemanticError("physical evidence does not cover the matched backends")
    if not isinstance(post_backends, dict) or set(post_backends) != set(_BACKENDS):
        raise SPEFSemanticError("post-physical evidence does not cover the matched backends")
    for backend in _BACKENDS:
        physical_runs = physical_backends[backend].get("runs")
        post_backend = post_backends[backend]
        if not isinstance(physical_runs, list) or len(physical_runs) != 2:
            raise SPEFSemanticError(f"{backend} physical attempt set is malformed")
        if not isinstance(post_backend, dict):
            raise SPEFSemanticError(f"{backend} post-physical result is malformed")
        if post_backend.get("passed") is not True:
            raise SPEFSemanticError(f"{backend} post-physical proof did not pass")
        if post_backend.get("both_physical_attempts_bound") is not True:
            raise SPEFSemanticError(f"{backend} post-physical proof did not bind both attempts")
        post_attempts = post_backend.get("attempts")
        if not isinstance(post_attempts, list) or len(post_attempts) != 2:
            raise SPEFSemanticError(f"{backend} post-physical attempt set is malformed")
        for attempt in (1, 2):
            physical_run = next(
                (
                    item
                    for item in physical_runs
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            post_attempt = next(
                (
                    item
                    for item in post_attempts
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            if physical_run is None or post_attempt is None:
                raise SPEFSemanticError(
                    f"{backend} attempt {attempt} prerequisite binding is missing"
                )
            physical_manifest = _require_digest(
                physical_run.get("manifest_sha256"),
                context=f"{backend} attempt {attempt} physical manifest",
            )
            post_manifest = _require_digest(
                post_attempt.get("physical_run_manifest", {}).get("sha256"),
                context=f"{backend} attempt {attempt} post-physical manifest",
            )
            if post_manifest != physical_manifest:
                raise SPEFSemanticError(
                    f"{backend} attempt {attempt} post-physical manifest binding differs"
                )

    return {
        "physical": physical,
        "physical_path": physical_path,
        "post_physical": post,
        "post_physical_path": post_path,
        "source_revision": actual_revision,
    }


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _mutate_declared_capacitance(text: str) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("*D_NET "):
            continue
        tokens = stripped.split()
        if len(tokens) != 3:
            continue
        value = Decimal(tokens[2])
        replacement = str(value + Decimal("1"))
        mutated = f"*D_NET {tokens[1]} {replacement}{_line_ending(line)}"
        lines[index] = mutated
        return "".join(lines), {
            "line_number": index + 1,
            "before": stripped,
            "after": mutated.strip(),
        }
    raise SPEFSemanticError("cannot construct declared-capacitance negative control")


def _mutate_resistance(text: str) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    in_resistance = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "*RES":
            in_resistance = True
            continue
        if in_resistance and stripped.startswith("*END"):
            in_resistance = False
        if not in_resistance or not stripped or stripped.startswith("*"):
            continue
        tokens = stripped.split()
        if len(tokens) != 4:
            continue
        value = Decimal(tokens[3])
        replacement = str(value + Decimal("1"))
        mutated = " ".join((*tokens[:3], replacement)) + _line_ending(line)
        lines[index] = mutated
        return "".join(lines), {
            "line_number": index + 1,
            "before": stripped,
            "after": mutated.strip(),
        }
    raise SPEFSemanticError("cannot construct resistance negative control")


def _mutate_unit(text: str) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("*C_UNIT "):
            continue
        tokens = stripped.split()
        if len(tokens) != 3:
            continue
        mutated = f"*C_UNIT {tokens[1]} FURLONG{_line_ending(line)}"
        lines[index] = mutated
        return "".join(lines), {
            "line_number": index + 1,
            "before": stripped,
            "after": mutated.strip(),
        }
    raise SPEFSemanticError("cannot construct unit negative control")


def _run_negative_control(
    *,
    fault: str,
    source_path: Path,
    baseline: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    text = source_path.read_text(encoding="utf-8")
    if fault == "declared_capacitance":
        mutated, mutation = _mutate_declared_capacitance(text)
        expected_message = "declared capacitance differs"
        mechanism = "parser_rejection"
    elif fault == "resistance":
        mutated, mutation = _mutate_resistance(text)
        expected_message = None
        mechanism = "canonical_digest_drift"
    elif fault == "unit":
        mutated, mutation = _mutate_unit(text)
        expected_message = "unsupported *C_UNIT unit"
        mechanism = "parser_rejection"
    else:
        raise ValueError(f"unsupported SPEF negative-control fault: {fault}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(mutated, encoding="utf-8")
    result: dict[str, Any] = {
        "fault": fault,
        "mechanism": mechanism,
        "source_sha256": _sha256(source_path),
        "mutated_sha256": _sha256(destination),
        "mutation": mutation,
        "mutated_file": destination.name,
    }
    if mechanism == "canonical_digest_drift":
        parsed = parse_spef_text(mutated)
        detected = parsed["canonical_sha256"] != baseline["canonical_sha256"]
        if not detected:
            raise SPEFSemanticError("resistance mutation did not change the canonical RC graph")
        result.update(
            {
                "detected": True,
                "baseline_canonical_sha256": baseline["canonical_sha256"],
                "mutated_canonical_sha256": parsed["canonical_sha256"],
            }
        )
        return result

    try:
        parse_spef_text(mutated)
    except SPEFSemanticError as exc:
        message = str(exc)
        if expected_message not in message:
            raise SPEFSemanticError(
                f"{fault} control failed for an unexpected reason: {message}"
            ) from exc
        result.update({"detected": True, "rejection": message})
        return result
    raise SPEFSemanticError(f"{fault} control was accepted")


def _collect_evidence(
    physical_root: Path,
    post_physical_root: Path,
    output: Path,
    *,
    source_revision: str | None,
) -> dict[str, Any]:
    chain = _validate_source_chain(
        physical_root,
        post_physical_root,
        source_revision=source_revision,
    )
    output.mkdir(parents=True)
    source_dir = output / "source"
    source_dir.mkdir()
    for source, name in (
        (chain["physical_path"], "openroad_physical_evidence.json"),
        (chain["post_physical_path"], "post_physical_equivalence_evidence.json"),
    ):
        _copy_bound(
            source,
            source_dir / name,
            expected_digest=_sha256(source),
            context=name,
        )

    evidence: dict[str, Any] = {
        "schema": _SCHEMA,
        "evidence_level": _EVIDENCE_LEVEL,
        "execution": _execution_context(chain["source_revision"]),
        "source": {
            "physical_evidence_sha256": _sha256(chain["physical_path"]),
            "post_physical_equivalence_evidence_sha256": _sha256(chain["post_physical_path"]),
        },
        "parser_contract": parser_contract(),
        "backends": {},
    }

    physical = chain["physical"]
    for backend in _BACKENDS:
        physical_backend = physical["backends"][backend]
        if physical_backend.get("repeatability", {}).get("passed") is not True:
            raise SPEFSemanticError(f"{backend} physical repeatability is not qualified")
        runs = physical_backend.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise SPEFSemanticError(f"{backend} must contain exactly two physical attempts")

        backend_dir = output / "backends" / backend
        attempts: list[dict[str, Any]] = []
        semantic_digests: list[str] = []
        normalized_digests: list[str] = []
        baseline_path: Path | None = None
        baseline_summary: dict[str, Any] | None = None
        for attempt in (1, 2):
            run = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            if run is None:
                raise SPEFSemanticError(f"{backend} attempt {attempt} is missing")
            manifest_digest = _require_digest(
                run.get("manifest_sha256"),
                context=f"{backend} attempt {attempt} manifest",
            )
            bound_manifest = _verify_file(
                physical_root / "evidence",
                run.get("manifest"),
                manifest_digest,
                context=f"{backend} attempt {attempt} bound run manifest",
            )
            attempt_root = (
                physical_root / "downloaded-runs" / f"openroad-physical-run-{backend}-{attempt}"
            )
            original_manifest = _verify_file(
                attempt_root,
                "openroad_run.json",
                manifest_digest,
                context=f"{backend} attempt {attempt} original run manifest",
            )
            if _sha256(bound_manifest) != _sha256(original_manifest):
                raise SPEFSemanticError(f"{backend} attempt {attempt} run-manifest copies differ")
            manifest = _load_json(bound_manifest)
            if manifest.get("schema") != "hephaestus.openroad-physical-run.v1":
                raise SPEFSemanticError(
                    f"{backend} attempt {attempt} has an unsupported run schema"
                )
            identity = manifest.get("identity")
            if not isinstance(identity, dict):
                raise SPEFSemanticError(f"{backend} attempt {attempt} identity is malformed")
            if identity.get("backend") != backend or identity.get("attempt") != attempt:
                raise SPEFSemanticError(f"{backend} attempt {attempt} identity differs")
            _require_claims(
                manifest.get("claims"),
                required_true=(
                    "registered_source_binding_verified",
                    "pinned_orfs_image_used",
                    "placement_performed",
                    "routing_performed",
                    "spef_generated",
                ),
                required_false=(
                    "post_physical_equivalence_verified",
                    "drc_clean",
                    "lvs_clean",
                    "power_estimated_with_activity",
                    "post_layout_pex_verified",
                    "foundry_signoff_complete",
                    "silicon_verified",
                ),
                context=f"{backend} attempt {attempt} run",
            )
            metadata = run.get("artifacts", {}).get("final_spef")
            if not isinstance(metadata, dict):
                raise SPEFSemanticError(f"{backend} attempt {attempt} SPEF metadata is missing")
            if manifest.get("artifacts", {}).get("final_spef") != metadata:
                raise SPEFSemanticError(
                    f"{backend} attempt {attempt} SPEF metadata differs from its manifest"
                )
            spef = _verify_file(
                attempt_root,
                metadata.get("path"),
                metadata.get("sha256"),
                context=f"{backend} attempt {attempt} routed SPEF",
            )
            size = metadata.get("size_bytes")
            if type(size) is not int or size != spef.stat().st_size:
                raise SPEFSemanticError(f"{backend} attempt {attempt} SPEF size binding differs")
            normalized_digest = _date_normalized_sha256(spef)
            expected_normalized = _require_digest(
                manifest.get("normalized", {}).get("spef_date_normalized_sha256"),
                context=f"{backend} attempt {attempt} normalized SPEF",
            )
            if normalized_digest != expected_normalized:
                raise SPEFSemanticError(
                    f"{backend} attempt {attempt} normalized SPEF digest differs"
                )
            normalized_digests.append(normalized_digest)

            summary = parse_spef(spef)
            semantic_digests.append(summary["canonical_sha256"])
            attempt_dir = backend_dir / f"attempt-{attempt}"
            attempt_dir.mkdir(parents=True)
            _copy_bound(
                spef,
                attempt_dir / "6_final.spef",
                expected_digest=_sha256(spef),
                context=f"{backend} attempt {attempt} SPEF",
            )
            _copy_bound(
                bound_manifest,
                attempt_dir / "openroad_run.json",
                expected_digest=manifest_digest,
                context=f"{backend} attempt {attempt} run manifest",
            )
            _write_json(attempt_dir / "semantic_summary.json", summary)
            attempts.append(
                {
                    "attempt": attempt,
                    "run_manifest_sha256": manifest_digest,
                    "raw_spef_sha256": _sha256(spef),
                    "raw_spef_size_bytes": size,
                    "date_normalized_spef_sha256": normalized_digest,
                    "semantic": summary,
                }
            )
            if attempt == 1:
                baseline_path = spef
                baseline_summary = summary

        if len(set(normalized_digests)) != 1:
            raise SPEFSemanticError(
                f"{backend} attempts differ after *DATE-only SPEF normalization"
            )
        if len(set(semantic_digests)) != 1:
            raise SPEFSemanticError(
                f"{backend} attempts differ after semantic SPEF canonicalization"
            )
        if baseline_path is None or baseline_summary is None:
            raise SPEFSemanticError(f"{backend} lacks a negative-control baseline")

        controls: dict[str, Any] = {}
        for fault in _FAULTS:
            controls[fault] = _run_negative_control(
                fault=fault,
                source_path=baseline_path,
                baseline=baseline_summary,
                destination=backend_dir / "negative-controls" / fault / "mutated.spef",
            )
        evidence["backends"][backend] = {
            "design": baseline_summary["design"],
            "spef_standard": baseline_summary["spef_standard"],
            "design_flow": baseline_summary["design_flow"],
            "delimiters": baseline_summary["delimiters"],
            "canonical_sha256": semantic_digests[0],
            "unit_contract": baseline_summary["unit_contract"],
            "metrics": baseline_summary["metrics"],
            "attempts": attempts,
            "negative_controls": controls,
            "passed": True,
        }

    evidence["claims"] = {
        "physical_spef_binding_verified": True,
        "post_physical_equivalence_prerequisite_verified": True,
        "all_six_spef_files_parsed": True,
        "spef_units_and_structure_validated": True,
        "spef_declared_capacitance_consistency_verified": True,
        "spef_semantic_repeatability_verified": True,
        "spef_negative_controls_detected": True,
        "fresh_parasitic_extraction_performed": False,
        "independent_pex_crosscheck_verified": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }
    return evidence


def build_evidence(
    physical_root: str | Path,
    post_physical_root: str | Path,
    reference_path: str | Path,
    output_dir: str | Path,
    *,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Build and qualify routed SPEF semantic evidence without overwriting inputs."""

    physical = _resolve_input_directory(Path(physical_root), context="physical artifact")
    post_physical = _resolve_input_directory(
        Path(post_physical_root),
        context="post-physical artifact",
    )
    reference_file = _resolve_input_file(
        Path(reference_path),
        context="SPEF semantic regression reference",
    )
    output = _resolve_output_directory(Path(output_dir))
    for label, protected in (
        ("physical artifact", physical),
        ("post-physical artifact", post_physical),
        ("regression reference", reference_file),
    ):
        if _paths_overlap(output, protected):
            raise SPEFSemanticError(
                f"output directory overlaps the {label}: {output} and {protected}"
            )

    reference = _load_reference(reference_file)
    evidence = _collect_evidence(
        physical,
        post_physical,
        output,
        source_revision=source_revision,
    )
    reference_digest = _sha256(reference_file)
    _copy_bound(
        reference_file,
        output / "source" / "regression_reference.json",
        expected_digest=reference_digest,
        context="SPEF semantic regression reference",
    )
    evidence["source"]["regression_reference_sha256"] = reference_digest
    evidence["regression"] = validate_reference(
        evidence,
        reference,
        reference_sha256=reference_digest,
    )
    _write_json(output / "spef_semantic_evidence.json", evidence)
    summary = [
        "# Qualified routed SPEF semantic evidence",
        "",
        f"- source revision: `{evidence['execution']['source_revision']}`",
        "- backends: `3`",
        "- physical SPEF attempts parsed: `6`",
        "- negative controls: `9`",
        "- semantic repeatability verified: `true`",
        "- fresh parasitic extraction performed: `false`",
        "- independent PEX cross-check verified: `false`",
        "- post-layout PEX verified: `false`",
        "- foundry sign-off / silicon: `false`",
        "",
    ]
    (output / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    return evidence


__all__ = ["build_evidence", "make_reference", "_collect_evidence"]
