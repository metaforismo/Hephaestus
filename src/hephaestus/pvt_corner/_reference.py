"""Stable regression projection and exact claim validation for routed PVT evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._common import (
    BACKENDS,
    CORNERS,
    EVIDENCE_LEVEL,
    PHYSICAL_ATTEMPTS,
    REFERENCE_ID,
    REFERENCE_SCHEMA,
    SCHEMA,
    PVTCornerError,
    load_json,
    sha256_json,
)

_MISSING = object()
_RUNTIME_TRUE_CLAIMS = (
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
)
_BOUNDARY_FALSE_CLAIMS = (
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
)
_COMPARATIVE_CLAIM = "comparative_pvt_claim_enabled"


def _expected_runtime_claims(*, comparative_enabled: bool) -> dict[str, bool]:
    return {
        **{name: True for name in _RUNTIME_TRUE_CLAIMS},
        _COMPARATIVE_CLAIM: comparative_enabled,
        **{name: False for name in _BOUNDARY_FALSE_CLAIMS},
    }


def _validate_runtime_claims(
    evidence: dict[str, Any],
    *,
    allowed_comparative_values: tuple[bool, ...],
) -> bool:
    """Require the exact supported claim set and return its comparative state."""

    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        raise PVTCornerError("PVT evidence claims are malformed")
    comparative = claims.get(_COMPARATIVE_CLAIM)
    if type(comparative) is not bool or comparative not in allowed_comparative_values:
        raise PVTCornerError(
            "PVT comparative claim has an invalid qualification state"
        )
    expected = _expected_runtime_claims(comparative_enabled=comparative)
    if claims != expected:
        missing = sorted(set(expected) - set(claims))
        unexpected = sorted(set(claims) - set(expected))
        wrong = sorted(
            name
            for name in set(expected) & set(claims)
            if claims[name] is not expected[name]
        )
        raise PVTCornerError(
            "PVT evidence claim boundary differs from the exact supported set: "
            f"missing={missing}, unexpected={unexpected}, wrong={wrong}"
        )
    return comparative


def _validate_evidence_identity(evidence: dict[str, Any]) -> None:
    if evidence.get("schema") != SCHEMA:
        raise PVTCornerError("unsupported PVT evidence schema")
    if evidence.get("evidence_level") != EVIDENCE_LEVEL:
        raise PVTCornerError("unexpected PVT evidence level")


def load_reference(path: Path) -> dict[str, Any]:
    reference = load_json(path)
    if reference.get("schema") != REFERENCE_SCHEMA:
        raise PVTCornerError("unsupported PVT reference schema")
    if reference.get("reference_id") != REFERENCE_ID:
        raise PVTCornerError("unexpected PVT reference identity")
    projection = reference.get("stable_projection")
    if not isinstance(projection, dict):
        raise PVTCornerError("PVT reference projection is malformed")
    return reference


def stable_projection(evidence: dict[str, Any]) -> dict[str, Any]:
    backends: dict[str, Any] = {}
    for backend in BACKENDS:
        value = evidence["backends"][backend]
        cases: dict[str, Any] = {}
        for attempt in PHYSICAL_ATTEMPTS:
            case = value["physical_attempts"][str(attempt)]
            cases[str(attempt)] = {
                "routed_verilog_sha256": case["routed_verilog_sha256"],
                "sdc_sha256": case["sdc_sha256"],
                "spef_date_normalized_sha256": case[
                    "spef_date_normalized_sha256"
                ],
                "corners": {
                    label: {
                        "liberty_sha256": case["corners"][label][
                            "liberty_sha256"
                        ],
                        "metrics": case["corners"][label]["metrics"],
                        "analysis_replays": len(
                            case["corners"][label]["replays"]
                        ),
                    }
                    for label in CORNERS
                },
                "negative_control": {
                    "clock_period_ns": case["negative_control"][
                        "clock_period_ns"
                    ],
                    "timing_violation_observed": case["negative_control"][
                        "timing_violation_observed"
                    ],
                    "metrics": case["negative_control"]["analysis"]["metrics"],
                },
            }
        backends[backend] = {
            "top_module": value["top_module"],
            "physical_attempts": cases,
            "physical_attempt_timing_repeatability_verified": value[
                "physical_attempt_timing_repeatability_verified"
            ],
        }
    return {
        "reference_id": REFERENCE_ID,
        "contract": {
            "contract_id": evidence["contract"]["value"]["contract_id"],
            "corner_order": evidence["corner_order"],
            "physical_attempts": evidence["contract"]["value"][
                "physical_attempts"
            ],
            "analysis_replays": evidence["contract"]["value"][
                "analysis_replays"
            ],
            "negative_control_clock_period_ns": evidence["contract"]["value"][
                "negative_control_clock_period_ns"
            ],
        },
        "toolchain": {
            "ihp_open_pdk_commit": evidence["toolchain"][
                "ihp_open_pdk_commit"
            ],
            "opensta_commit": evidence["toolchain"]["opensta_commit"],
            "opensta_banner": evidence["toolchain"]["opensta_banner"],
            "liberty_sha256": {
                label: evidence["toolchain"]["liberty"][label]["sha256"]
                for label in CORNERS
            },
        },
        "backends": backends,
        # A bootstrap artifact must keep this false. The reference records the
        # exact final boundary that becomes eligible only after validation.
        "claim_boundary": _expected_runtime_claims(comparative_enabled=True),
    }


def make_reference(evidence: dict[str, Any]) -> dict[str, Any]:
    """Create a reference only from an exact, unpromoted bootstrap artifact."""

    _validate_evidence_identity(evidence)
    comparative = _validate_runtime_claims(
        evidence,
        allowed_comparative_values=(False,),
    )
    if comparative is not False:
        raise PVTCornerError("PVT bootstrap evidence is already promoted")
    if evidence.get("regression") != {
        "passed": False,
        "bootstrap_reference_required": True,
    }:
        raise PVTCornerError(
            "PVT reference requires the exact unqualified bootstrap regression state"
        )
    return {
        "schema": REFERENCE_SCHEMA,
        "reference_id": REFERENCE_ID,
        "stable_projection": stable_projection(evidence),
    }


def _render(value: object) -> str:
    if value is _MISSING:
        return "<missing>"
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _differences(expected: object, actual: object, *, path: str = "$") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        values: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            values.extend(
                _differences(
                    expected.get(key, _MISSING),
                    actual.get(key, _MISSING),
                    path=f"{path}.{key}",
                )
            )
        return values
    if isinstance(expected, list) and isinstance(actual, list):
        values = []
        if len(expected) != len(actual):
            values.append(
                f"{path}.length: expected={len(expected)}, actual={len(actual)}"
            )
        for index in range(max(len(expected), len(actual))):
            lhs = expected[index] if index < len(expected) else _MISSING
            rhs = actual[index] if index < len(actual) else _MISSING
            values.extend(_differences(lhs, rhs, path=f"{path}[{index}]"))
        return values
    if expected == actual:
        return []
    return [f"{path}: expected={_render(expected)}, actual={_render(actual)}"]


def validate_reference(
    evidence: dict[str, Any],
    reference: dict[str, Any],
    *,
    reference_sha256: str,
) -> dict[str, Any]:
    """Validate either pre-promotion evidence or a replayed final artifact."""

    _validate_evidence_identity(evidence)
    comparative = _validate_runtime_claims(
        evidence,
        allowed_comparative_values=(False, True),
    )
    if comparative is True:
        regression = evidence.get("regression")
        if not isinstance(regression, dict) or regression.get("passed") is not True:
            raise PVTCornerError(
                "promoted PVT evidence lacks a successful regression result"
            )

    expected = reference["stable_projection"]
    actual = stable_projection(evidence)
    if expected != actual:
        differences = _differences(expected, actual)
        preview = "\n".join(differences[:40])
        suffix = (
            ""
            if len(differences) <= 40
            else f"\n... {len(differences) - 40} more"
        )
        raise PVTCornerError(
            "PVT projection differs from the pinned reference: "
            f"{len(differences)} field(s); "
            f"expected_sha256={sha256_json(expected)}, "
            f"actual_sha256={sha256_json(actual)}\n{preview}{suffix}"
        )
    return {
        "reference_id": REFERENCE_ID,
        "reference_sha256": reference_sha256,
        "stable_projection_sha256": sha256_json(actual),
        "passed": True,
    }
