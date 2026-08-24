from __future__ import annotations

from pathlib import Path

import pytest

import hephaestus.post_physical_equivalence as ppe
from hephaestus.post_physical_equivalence import _common


def test_resolve_under_rejects_a_direct_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.v"
    target.write_text("module target; endmodule\n", encoding="utf-8")
    link = root / "linked.v"
    link.symlink_to(target.name)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="symlinks"):
        _common._resolve_under(root, link.name, context="direct link")


def test_resolve_under_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real"
    real.mkdir()
    source = real / "source.v"
    source.write_text("module source; endmodule\n", encoding="utf-8")
    alias = root / "alias"
    alias.symlink_to(real.name, target_is_directory=True)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="symlinks"):
        _common._resolve_under(root, "alias/source.v", context="parent link")


def test_resolve_under_rejects_parent_traversal_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="parent traversal"):
        _common._resolve_under(root, "../outside.v", context="escape")
