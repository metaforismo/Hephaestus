"""Stable regression projection and execution-context metadata."""

from __future__ import annotations

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


def _load_reference(path: Path) -> dict[str, Any]:
    reference = _load_json(path)
    if reference.get("schema") != _REFERENCE_SCHEMA:
        raise PostPhysicalEquivalenceError("unsupported post-physical reference schema")
    if reference.get("reference_id") != _REFERENCE_ID:
        raise PostPhysicalEquivalenceError("unexpected post-physical reference identity")
    return reference


def _stable_projection(evidence: dict[str, Any]) -> dict[str, Any]:
    backends: dict[str, Any] = {}
    for backend in _BACKENDS:
        value = evidence["backends"][backend]
        attempts = value["attempts"]
        backends[backend] = {
            "source_core_sha256": value["source_core"]["sha256"],
            "source_wrapper_sha256": value["source_wrapper"]["sha256"],
            "routed_verilog_sha256": [
                item["routed_verilog"]["sha256"] for item in attempts
            ],
            "gate_wrapper_sha256": [item["gate_wrapper_sha256"] for item in attempts],
            "reset_synchronized_base_case": [
                {
                    "script_sha256": item["reset_synchronized_base_case"][
                        "script_sha256"
                    ],
                    "equiv_cells_total": item["reset_synchronized_base_case"][
                        "equiv_cells_total"
                    ],
                    "proof_success": item["reset_synchronized_base_case"][
                        "proof_success"
                    ],
                }
                for item in attempts
            ],
            "steady_state_induction": [
                {
                    "script_sha256": item["steady_state_induction"]["script_sha256"],
                    "equiv_cells_total": item["steady_state_induction"][
                        "equiv_cells_total"
                    ],
                    "equiv_cells_proven": item["steady_state_induction"][
                        "equiv_cells_proven"
                    ],
                    "equiv_cells_unproven": item["steady_state_induction"][
                        "equiv_cells_unproven"
                    ],
                }
                for item in attempts
            ],
            "negative_controls": {
                fault: {
                    "wrapper_sha256": value["negative_controls"][fault][
                        "wrapper_sha256"
                    ],
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
                    "steady_state_induction": {
                        "script_sha256": value["negative_controls"][fault][
                            "steady_state_induction"
                        ]["script_sha256"],
                        "negative_unproven_cells": value["negative_controls"][fault][
                            "steady_state_induction"
                        ]["negative_unproven_cells"],
                    },
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


def _validate_reference(evidence: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    expected = reference.get("stable_projection")
    if not isinstance(expected, dict):
        raise PostPhysicalEquivalenceError("post-physical reference projection is malformed")
    actual = _stable_projection(evidence)
    if actual != expected:
        raise PostPhysicalEquivalenceError(
            "post-physical stable projection differs from the pinned regression reference"
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
