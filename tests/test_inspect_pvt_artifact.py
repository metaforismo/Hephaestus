from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "inspect_pvt_artifact.py"
_SPEC = importlib.util.spec_from_file_location("inspect_pvt_artifact", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_diagnostics_rejects_opensta_warnings_and_errors(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("Warning: unconstrained endpoint\n", encoding="utf-8")
    stderr.write_text("%Error: failed annotation\n", encoding="utf-8")

    assert _MODULE._diagnostics(stdout, stderr) == [
        "Warning: unconstrained endpoint",
        "%Error: failed annotation",
    ]


def test_diagnostics_accepts_a_clean_report(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("0.25 slack (MET)\ntns 0.0\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")

    assert _MODULE._diagnostics(stdout, stderr) == []


def test_safe_root_rejects_a_symlinked_artifact_member(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("evidence\n", encoding="utf-8")
    (root / "linked.txt").symlink_to(target.name)

    with pytest.raises(_MODULE.InspectionError, match="contains a symlink"):
        _MODULE._safe_root(root)


def test_tree_digest_is_path_sensitive_and_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("one\n", encoding="utf-8")
    (first / "b.txt").write_text("two\n", encoding="utf-8")
    (second / "b.txt").write_text("two\n", encoding="utf-8")
    (second / "a.txt").write_text("one\n", encoding="utf-8")

    assert _MODULE._tree_sha256(first) == _MODULE._tree_sha256(second)
    (second / "a.txt").rename(second / "c.txt")
    assert _MODULE._tree_sha256(first) != _MODULE._tree_sha256(second)


def test_inspector_rejects_a_non_git_source_revision(tmp_path: Path) -> None:
    with pytest.raises(_MODULE.InspectionError, match="40-character Git SHA"):
        _MODULE.inspect_artifact(
            tmp_path,
            expected_source_revision="not-a-revision",
            strict=False,
        )
