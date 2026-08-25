"""Stable regression projection for routed SPEF semantic evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._common import (
    _BACKENDS,
    _FAULTS,
    _REFERENCE_ID,
    _REFERENCE_SCHEMA,
    SPEFSemanticError,
    _load_json,
    _sha256_json,
)

_MISSING = object()


def _load_reference(path: Path) -> dict[str, Any]:
    reference = _load_json(path)
    if reference.get("schema") != _REFERENCE_SCHEMA:
        raise SPEFSemanticError("unsupported SPEF semantic reference schema")
    if reference.get("reference_id") != _REFERENCE_ID:
        raise SPEFSemanticError("unexpected SPEF semantic reference identity")
    projection = reference.get("stable_projection")
    if not isinstance(projection, dict):
        raise SPEFSemanticError("SPEF semantic reference projection is malformed")
    return reference


def _stable_projection(evidence: dict[str, Any]) -> dict[str, Any]:
    backends: dict[str, Any] = {}
    for backend in _BACKENDS:
        value = evidence["backends"][backend]
        attempts = value["attempts"]
        controls = value["negative_controls"]
        backends[backend] = {
            "attempt_count": len(attempts),
            "canonical_sha256": value["canonical_sha256"],
            "design": value["design"],
            "spef_standard": value["spef_standard"],
            "design_flow": value["design_flow"],
            "delimiters": value["delimiters"],
            "unit_contract": value["unit_contract"],
            "metrics": value["metrics"],
            "negative_controls": {
                fault: {
                    "detected": controls[fault]["detected"],
                    "mechanism": controls[fault]["mechanism"],
                }
                for fault in _FAULTS
            },
        }
    return {
        "reference_id": _REFERENCE_ID,
        "parser_contract": evidence["parser_contract"],
        "backends": backends,
        "claims": evidence["claims"],
    }


def make_reference(evidence: dict[str, Any]) -> dict[str, Any]:
    """Create a reviewable regression reference from already-collected evidence."""

    return {
        "schema": _REFERENCE_SCHEMA,
        "reference_id": _REFERENCE_ID,
        "stable_projection": _stable_projection(evidence),
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
    actual = _stable_projection(evidence)
    if expected != actual:
        differences = _differences(expected, actual)
        preview = "\n".join(differences[:40])
        suffix = "" if len(differences) <= 40 else f"\n... {len(differences) - 40} more"
        raise SPEFSemanticError(
            "SPEF semantic projection differs from the pinned reference: "
            f"{len(differences)} field(s); "
            f"expected_sha256={_sha256_json(expected)}, "
            f"actual_sha256={_sha256_json(actual)}\n{preview}{suffix}"
        )
    return {
        "reference_id": _REFERENCE_ID,
        "reference_sha256": reference_sha256,
        "stable_projection_sha256": _sha256_json(actual),
        "passed": True,
    }
