"""Qualified routed IHP SG13G2 multi-corner timing evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._builder import (
    build_evidence as _build_evidence,
    build_reference,
    validate_existing_reference,
)
from ._common import PVTCornerError
from ._opensta import emit_opensta_script, parse_opensta_output, tighten_sdc
from ._provenance import validate_upstream_run_binding
from ._source import validate_contract, validate_source_chain


def build_evidence(
    physical_root: str | Path,
    post_physical_root: str | Path,
    pdk_root: str | Path,
    opensta_executable: str | Path,
    opensta_manifest_path: str | Path,
    contract_path: str | Path,
    output_dir: str | Path,
    *,
    source_revision: str,
    reference_path: str | Path | None = None,
    upstream_run_id: str | None = None,
) -> dict[str, Any]:
    """Build PVT evidence after proving any recorded upstream run identity."""

    validate_upstream_run_binding(post_physical_root, upstream_run_id)
    return _build_evidence(
        physical_root,
        post_physical_root,
        pdk_root,
        opensta_executable,
        opensta_manifest_path,
        contract_path,
        output_dir,
        source_revision=source_revision,
        reference_path=reference_path,
        upstream_run_id=upstream_run_id,
    )


__all__ = [
    "PVTCornerError",
    "build_evidence",
    "build_reference",
    "emit_opensta_script",
    "parse_opensta_output",
    "tighten_sdc",
    "validate_contract",
    "validate_existing_reference",
    "validate_source_chain",
    "validate_upstream_run_binding",
]
