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

    root = Path(physical_root).resolve()
    models = Path(models_path).resolve()
    reference = Path(reference_path).resolve()
    output = Path(output_dir).resolve()
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
