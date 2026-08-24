from __future__ import annotations

from pathlib import Path

import pytest

import hephaestus.post_physical_equivalence as ppe
from hephaestus.post_physical_equivalence import _common, _source


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


def test_source_chain_rejects_a_symlinked_root_manifest(tmp_path: Path) -> None:
    root = tmp_path / "physical"
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    manifest = evidence_dir / "openroad_physical_evidence.json"
    manifest.symlink_to(outside)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="symlinks"):
        _source._validate_source_chain(root)


def test_public_builder_rejects_symlinked_models_before_reading_inputs(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models.v"
    models.write_text("module model; endmodule\n", encoding="utf-8")
    models_link = tmp_path / "models-link.v"
    models_link.symlink_to(models.name)
    reference = tmp_path / "reference.json"
    reference.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        ppe.PostPhysicalEquivalenceError,
        match="functional cell models path must not contain symlinks",
    ):
        ppe.build_evidence(
            tmp_path / "physical",
            models_link,
            reference,
            tmp_path / "output",
        )


def test_public_builder_rejects_a_symlinked_output(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    models = tmp_path / "models.v"
    models.write_text("module model; endmodule\n", encoding="utf-8")
    reference = tmp_path / "reference.json"
    reference.write_text("{}\n", encoding="utf-8")
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output.name, target_is_directory=True)

    with pytest.raises(
        ppe.PostPhysicalEquivalenceError,
        match="output directory path must not contain symlinks",
    ):
        ppe.build_evidence(
            physical,
            models,
            reference,
            output_link,
        )
