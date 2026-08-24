"""Stable regression projection and execution-context metadata."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from ._common import (
    _BACKENDS,
    _FAULTS,
    _GIT_SHA_RE,
    _REFERENCE_ID,
    _REFERENCE_SCHEMA,
    PostPhysicalEquivalenceError,
    _load_json,
    _sha256_text,
)

_MISSING = object()


def _load_reference(path: Path) -> dict[str, Any]:
    reference = _load_json(path)
    if reference.get("schema") != _REFERENCE_SCHEMA:
        raise PostPhysicalEquivalenceError("unsupported post-physical reference schema")
    if reference.get("reference_id") != _REFERENCE_ID:
        raise PostPhysicalEquivalenceError("unexpected post-physical reference identity")
    return reference


def _negative_induction_projection(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PostPhysicalEquivalenceError(f"{context} result is malformed")
    count = value.get("negative_unproven_cells")
    if type(count) is not int or count <= 0:
        raise PostPhysicalEquivalenceError(
            f"{context} must record at least one unproven equivalence cell"
        )
    if value.get("passed") is not True:
        raise PostPhysicalEquivalenceError(f"{context} did not pass its negative-control gate")
    return {
        "negative_control_passed": True,
        "script_sha256": value["script_sha256"],
        "unproven_equivalence_detected": True,
    }


def _stable_projection(evidence: dict[str, Any]) -> dict[str, Any]:
    backends: dict[str, Any] = {}
    for backend in _BACKENDS:
        value = evidence["backends"][backend]
        attempts = value["attempts"]
        backends[backend] = {
            "source_core_sha256": value["source_core"]["sha256"],
            "source_wrapper_sha256": value["source_wrapper"]["sha256"],
            "routed_verilog_sha256": [item["routed_verilog"]["sha256"] for item in attempts],
            "gate_wrapper_sha256": [item["gate_wrapper_sha256"] for item in attempts],
            "reset_synchronized_base_case": [
                {
                    "script_sha256": item["reset_synchronized_base_case"]["script_sha256"],
                    "equiv_cells_total": item["reset_synchronized_base_case"]["equiv_cells_total"],
                    "proof_success": item["reset_synchronized_base_case"]["proof_success"],
                }
                for item in attempts
            ],
            "steady_state_induction": [
                {
                    "script_sha256": item["steady_state_induction"]["script_sha256"],
                    "equiv_cells_total": item["steady_state_induction"]["equiv_cells_total"],
                    "equiv_cells_proven": item["steady_state_induction"]["equiv_cells_proven"],
                    "equiv_cells_unproven": item["steady_state_induction"]["equiv_cells_unproven"],
                }
                for item in attempts
            ],
            "negative_controls": {
                fault: {
                    "wrapper_sha256": value["negative_controls"][fault]["wrapper_sha256"],
                    "reset_synchronized_base_case": {
                        "script_sha256": value["negative_controls"][fault][
                            "reset_synchronized_base_case"
                        ]["script_sha256"],
                        "equiv_cells_total": value["negative_controls"][fault][
                            "reset_synchronized_base_case"
                        ]["equiv_cells_total"],
                        "counterexample_found": value["negative_controls"][fault][
                            "reset_synchronized_base_case"
                        ]["counterexample_found"],
                    },
                    "steady_state_induction": _negative_induction_projection(
                        value["negative_controls"][fault]["steady_state_induction"],
                        context=f"{backend}.{fault}.steady_state_induction",
                    ),
                }
                for fault in _FAULTS
            },
        }
    return {
        "reference_id": _REFERENCE_ID,
        "proof_contract": evidence["proof_contract"],
        "functional_cell_models_sha256": evidence["source"]["functional_cell_models_sha256"],
        "yosys_version": evidence["toolchain"]["version"],
        "backends": backends,
        "claims": evidence["claims"],
    }


def _canonicalize_projection(
    projection: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    """Normalize legacy exact counts into the stable negative-control predicate."""

    canonical = copy.deepcopy(projection)
    backends = canonical.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise PostPhysicalEquivalenceError(f"{context} backend projection is malformed")
    for backend in _BACKENDS:
        backend_value = backends[backend]
        if not isinstance(backend_value, dict):
            raise PostPhysicalEquivalenceError(
                f"{context}.{backend} backend projection is malformed"
            )
        controls = backend_value.get("negative_controls")
        if not isinstance(controls, dict) or set(controls) != set(_FAULTS):
            raise PostPhysicalEquivalenceError(
                f"{context}.{backend} negative-control projection is malformed"
            )
        for fault in _FAULTS:
            control = controls[fault]
            if not isinstance(control, dict):
                raise PostPhysicalEquivalenceError(
                    f"{context}.{backend}.{fault} control projection is malformed"
                )
            steady = control.get("steady_state_induction")
            if not isinstance(steady, dict):
                raise PostPhysicalEquivalenceError(
                    f"{context}.{backend}.{fault} induction projection is malformed"
                )
            legacy_count = steady.pop("negative_unproven_cells", None)
            if legacy_count is not None:
                if type(legacy_count) is not int or legacy_count <= 0:
                    raise PostPhysicalEquivalenceError(
                        f"{context}.{backend}.{fault} legacy unproven count is invalid"
                    )
                for field in (
                    "negative_control_passed",
                    "unproven_equivalence_detected",
                ):
                    if field in steady and steady[field] is not True:
                        raise PostPhysicalEquivalenceError(
                            f"{context}.{backend}.{fault} legacy count contradicts {field}"
                        )
                steady["negative_control_passed"] = True
                steady["unproven_equivalence_detected"] = True
            if steady.get("negative_control_passed") is not True:
                raise PostPhysicalEquivalenceError(
                    f"{context}.{backend}.{fault} negative control is not qualified"
                )
            if steady.get("unproven_equivalence_detected") is not True:
                raise PostPhysicalEquivalenceError(
                    f"{context}.{backend}.{fault} did not preserve an unproven point"
                )
    return canonical


def _render_difference_value(value: object) -> str:
    if value is _MISSING:
        return "<missing>"
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _projection_differences(
    expected: object,
    actual: object,
    *,
    path: str = "$",
) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            differences.extend(
                _projection_differences(
                    expected.get(key, _MISSING),
                    actual.get(key, _MISSING),
                    path=f"{path}.{key}",
                )
            )
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        differences = []
        if len(expected) != len(actual):
            differences.append(f"{path}.length: expected={len(expected)}, actual={len(actual)}")
        for index in range(max(len(expected), len(actual))):
            expected_item = expected[index] if index < len(expected) else _MISSING
            actual_item = actual[index] if index < len(actual) else _MISSING
            differences.extend(
                _projection_differences(
                    expected_item,
                    actual_item,
                    path=f"{path}[{index}]",
                )
            )
        return differences
    if expected == actual:
        return []
    return [
        f"{path}: expected={_render_difference_value(expected)}, "
        f"actual={_render_difference_value(actual)}"
    ]


def _validate_reference(evidence: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    expected_value = reference.get("stable_projection")
    if not isinstance(expected_value, dict):
        raise PostPhysicalEquivalenceError("post-physical reference projection is malformed")
    expected = _canonicalize_projection(expected_value, context="reference")
    actual = _canonicalize_projection(_stable_projection(evidence), context="evidence")
    if actual != expected:
        differences = _projection_differences(expected, actual)
        expected_json = json.dumps(expected, separators=(",", ":"), sort_keys=True)
        actual_json = json.dumps(actual, separators=(",", ":"), sort_keys=True)
        preview = "\n".join(differences[:40])
        suffix = "" if len(differences) <= 40 else f"\n... {len(differences) - 40} more"
        raise PostPhysicalEquivalenceError(
            "post-physical stable projection differs from the pinned regression reference: "
            f"{len(differences)} field(s); "
            f"expected_sha256={_sha256_text(expected_json)}, "
            f"actual_sha256={_sha256_text(actual_json)}\n"
            f"{preview}{suffix}"
        )
    return {
        "reference_id": _REFERENCE_ID,
        "reference_sha256": evidence["source"]["regression_reference_sha256"],
        "stable_projection_sha256": _sha256_text(
            json.dumps(actual, separators=(",", ":"), sort_keys=True)
        ),
        "passed": True,
    }


def _execution_context(source_revision: str | None) -> dict[str, Any]:
    revision = source_revision or os.environ.get("GITHUB_SHA")
    if revision is not None and _GIT_SHA_RE.fullmatch(revision) is None:
        raise PostPhysicalEquivalenceError(
            "source revision must be a lowercase 40-character Git SHA"
        )
    return {
        "source_revision": revision,
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
    }
