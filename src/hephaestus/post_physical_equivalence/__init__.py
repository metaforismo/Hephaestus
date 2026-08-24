"""Qualified sequential equivalence for routed registered tiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._builder import build_evidence as _build_evidence
from ._common import PostPhysicalEquivalenceError
from ._proof import (
    emit_bounded_reset_script,
    emit_equivalence_script,
    emit_fault_wrapper,
    emit_passthrough_wrapper,
)


def _paths_overlap(lhs: Path, rhs: Path) -> bool:
    return lhs == rhs or lhs in rhs.parents or rhs in lhs.parents


def _resolve_without_symlinks(value: Path, *, context: str) -> Path:
    absolute = Path(value).absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise PostPhysicalEquivalenceError(
                f"{context} path must not contain symlinks: {absolute}"
            )
    return absolute.resolve()


def build_evidence(
    physical_root: Path,
    models_path: Path,
    reference_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    timeout: int = 300,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Build evidence without deleting or overwriting caller-owned paths."""

    root = _resolve_without_symlinks(Path(physical_root), context="physical artifact")
    models = _resolve_without_symlinks(Path(models_path), context="functional cell models")
    reference = _resolve_without_symlinks(Path(reference_path), context="regression reference")
    output = _resolve_without_symlinks(Path(output_dir), context="output directory")
    protected = {
        "physical artifact": root,
        "functional cell models": models,
        "regression reference": reference,
    }
    for label, path in protected.items():
        if _paths_overlap(output, path):
            raise PostPhysicalEquivalenceError(
                f"output directory overlaps the {label}: {output} and {path}"
            )
    if output.exists():
        raise PostPhysicalEquivalenceError(
            f"output directory already exists; refusing destructive replacement: {output}"
        )
    return _build_evidence(
        root,
        models,
        reference,
        output,
        yosys=yosys,
        timeout=timeout,
        source_revision=source_revision,
    )


__all__ = [
    "PostPhysicalEquivalenceError",
    "build_evidence",
    "emit_bounded_reset_script",
    "emit_equivalence_script",
    "emit_fault_wrapper",
    "emit_passthrough_wrapper",
]
