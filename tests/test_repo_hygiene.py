from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "check_repo_hygiene.py"
SPEC = importlib.util.spec_from_file_location("check_repo_hygiene", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)


def test_permanent_workflows_are_allowed() -> None:
    assert hygiene.violations(
        [
            PurePosixPath(".github/workflows/ci.yml"),
            PurePosixPath(".github/workflows/openroad-physical-evidence.yml"),
        ]
    ) == []


def test_temporary_workflow_prefixes_are_rejected() -> None:
    failures = hygiene.violations(
        [
            PurePosixPath(".github/workflows/one-shot-cleanup.yml"),
            PurePosixPath(".github/workflows/research-probe.yml"),
            PurePosixPath(".github/workflows/promote-reference.yml"),
        ]
    )

    assert failures == [
        "temporary qualification workflow is tracked: "
        ".github/workflows/one-shot-cleanup.yml",
        "temporary qualification workflow is tracked: "
        ".github/workflows/promote-reference.yml",
        "temporary qualification workflow is tracked: "
        ".github/workflows/research-probe.yml",
    ]


def test_temporary_payload_directories_are_rejected() -> None:
    failures = hygiene.violations(
        [
            PurePosixPath(".github/openroad-physical-parts/module.part"),
            PurePosixPath(".github/post-physical-payload/payload.b64"),
        ]
    )

    assert failures == [
        "temporary qualification payload is tracked: "
        ".github/openroad-physical-parts/module.part",
        "temporary qualification payload is tracked: "
        ".github/post-physical-payload/payload.b64",
    ]


def test_generated_physical_artifacts_remain_rejected() -> None:
    assert hygiene.violations([PurePosixPath("results/tile.gds")]) == [
        "generated or PDK artifact is tracked: results/tile.gds"
    ]
