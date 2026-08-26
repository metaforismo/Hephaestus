"""Stable regression projection for routed PVT evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._common import (
    BACKENDS,
    CORNERS,
    PHYSICAL_ATTEMPTS,
    REFERENCE_ID,
    REFERENCE_SCHEMA,
    PVTCornerError,
    load_json,
    sha256_json,
)

_MISSING = object()


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
                "spef_date_normalized_sha256": case["spef_date_normalized_sha256"],
                "corners": {
                    label: {
                        "liberty_sha256": case["corners"][label]["liberty_sha256"],
                        "metrics": case["corners"][label]["metrics"],
                        "analysis_replays": len(case["corners"][label]["replays"]),
                    }
                    for label in CORNERS
                },
                "negative_control": {
                    "clock_period_ns": case["negative_control"]["clock_period_ns"],
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
            "physical_attempts": evidence["contract"]["value"]["physical_attempts"],
            "analysis_replays": evidence["contract"]["value"]["analysis_replays"],
            "negative_control_clock_period_ns": evidence["contract"]["value"][
                "negative_control_clock_period_ns"
            ],
        },
        "toolchain": {
            "ihp_open_pdk_commit": evidence["toolchain"]["ihp_open_pdk_commit"],
            "opensta_commit": evidence["toolchain"]["opensta_commit"],
            "opensta_banner": evidence["toolchain"]["opensta_banner"],
            "liberty_sha256": {
                label: evidence["toolchain"]["liberty"][label]["sha256"] for label in CORNERS
            },
        },
        "backends": backends,
        "claim_boundary": {
            "comparative_pvt_claim_enabled": True,
            "ocv_analyzed": False,
            "aocv_analyzed": False,
            "pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "ir_drop_analyzed": False,
            "electromigration_analyzed": False,
            "thermal_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }


def make_reference(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": REFERENCE_SCHEMA,
        "reference_id": REFERENCE_ID,
        "stable_projection": stable_projection(evidence),
    }


def _render(value: object) -> str:
    if value is _MISSING:
        return "<missing>"
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


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
            values.append(f"{path}.length: expected={len(expected)}, actual={len(actual)}")
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
    expected = reference["stable_projection"]
    actual = stable_projection(evidence)
    if expected != actual:
        differences = _differences(expected, actual)
        preview = "\n".join(differences[:40])
        suffix = "" if len(differences) <= 40 else f"\n... {len(differences) - 40} more"
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
