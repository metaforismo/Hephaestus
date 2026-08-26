from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus import pvt_corner


def _artifact(
    tmp_path: Path,
    *,
    run_id: str = "123",
    workflow_ref: str = (
        "metaforismo/Hephaestus/.github/workflows/"
        "openroad-physical-evidence.yml@refs/pull/42/merge"
    ),
) -> Path:
    root = tmp_path / "post"
    root.mkdir()
    value = {
        "schema": "hephaestus.post-physical-equivalence-evidence.v1",
        "evidence_level": (
            "exact_registered_source_to_routed_sequential_equivalence"
        ),
        "execution": {
            "github_run_id": run_id,
            "github_workflow_ref": workflow_ref,
        },
    }
    (root / "post_physical_equivalence_evidence.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def test_upstream_run_binding_matches_the_artifact_provenance(tmp_path: Path) -> None:
    root = _artifact(tmp_path)

    value = pvt_corner.validate_upstream_run_binding(root, "123")

    assert value is not None
    assert value["upstream_physical_workflow_run_id"] == "123"
    assert len(value["post_physical_manifest_sha256"]) == 64
    assert "openroad-physical-evidence.yml@" in value[
        "post_physical_workflow_ref"
    ]


def test_upstream_run_binding_rejects_cross_run_substitution(tmp_path: Path) -> None:
    root = _artifact(tmp_path, run_id="123")

    with pytest.raises(
        pvt_corner.PVTCornerError,
        match="came from another workflow run",
    ):
        pvt_corner.validate_upstream_run_binding(root, "456")


def test_upstream_run_binding_rejects_another_workflow(tmp_path: Path) -> None:
    root = _artifact(
        tmp_path,
        workflow_ref=(
            "metaforismo/Hephaestus/.github/workflows/"
            "unrelated.yml@refs/pull/42/merge"
        ),
    )

    with pytest.raises(
        pvt_corner.PVTCornerError,
        match="permanent physical workflow",
    ):
        pvt_corner.validate_upstream_run_binding(root, "123")


def test_local_exploration_may_omit_a_github_run_id(tmp_path: Path) -> None:
    root = _artifact(tmp_path, run_id="not-used")

    assert pvt_corner.validate_upstream_run_binding(root, None) is None
