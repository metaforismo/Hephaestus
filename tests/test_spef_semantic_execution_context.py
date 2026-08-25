from __future__ import annotations

import pytest

import hephaestus.spef_semantic as spef_semantic
from hephaestus.spef_semantic import _common

_REVISION = "1" * 40


def test_execution_context_records_a_valid_upstream_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEPHAESTUS_UPSTREAM_PHYSICAL_RUN_ID", "12345")

    value = _common._execution_context(_REVISION)

    assert value["upstream_physical_workflow_run_id"] == "12345"


def test_execution_context_rejects_an_invalid_upstream_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEPHAESTUS_UPSTREAM_PHYSICAL_RUN_ID", "run-123")

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="positive integer",
    ):
        _common._execution_context(_REVISION)
