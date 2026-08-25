"""Shared safety, validation, and provenance helpers for SPEF evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_FAULTS = ("declared_capacitance", "resistance", "unit")
_SCHEMA = "hephaestus.spef-semantic-evidence.v1"
_EVIDENCE_LEVEL = "bound_routed_spef_semantic_repeatability"
_REFERENCE_SCHEMA = "hephaestus.spef-semantic-reference.v1"
_REFERENCE_ID = "ihp-sg13g2-spef-semantic-tiny-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


class SPEFSemanticError(RuntimeError):
    """Raised when a routed SPEF cannot satisfy the permanent contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SPEFSemanticError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SPEFSemanticError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SPEFSemanticError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _require_revision(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise SPEFSemanticError(f"{context} must be a lowercase 40-character Git SHA")
    return value


def _require_claims(
    claims: Any,
    *,
    required_true: tuple[str, ...],
    required_false: tuple[str, ...],
    context: str,
) -> dict[str, Any]:
    if not isinstance(claims, dict):
        raise SPEFSemanticError(f"{context} claims are malformed")
    missing_true = [name for name in required_true if claims.get(name) is not True]
    missing_false = [name for name in required_false if claims.get(name) is not False]
    if missing_true or missing_false:
        raise SPEFSemanticError(
            f"{context} claim boundary is invalid: true={missing_true}, false={missing_false}"
        )
    return claims


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(candidate: Path, *, context: str) -> None:
    absolute = _absolute_without_symlink_resolution(candidate)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise SPEFSemanticError(f"{context} path must not contain symlinks: {absolute}")


def _resolve_input_directory(path: Path, *, context: str) -> Path:
    candidate = _absolute_without_symlink_resolution(path)
    _reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise SPEFSemanticError(f"{context} is not a directory: {resolved}")
    return resolved


def _resolve_input_file(path: Path, *, context: str) -> Path:
    candidate = _absolute_without_symlink_resolution(path)
    _reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise SPEFSemanticError(f"{context} is not a non-empty regular file: {resolved}")
    return resolved


def _resolve_output_directory(path: Path) -> Path:
    candidate = _absolute_without_symlink_resolution(path)
    _reject_symlink_components(candidate.parent, context="output directory parent")
    resolved = candidate.resolve()
    if resolved.exists():
        raise SPEFSemanticError(
            f"output directory already exists; refusing destructive replacement: {resolved}"
        )
    return resolved


def _paths_overlap(lhs: Path, rhs: Path) -> bool:
    return lhs == rhs or lhs in rhs.parents or rhs in lhs.parents


def _resolve_under(root: Path, relative: Any, *, context: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SPEFSemanticError(f"{context} path must be a non-empty string")
    raw = Path(relative)
    if raw.is_absolute():
        raise SPEFSemanticError(f"{context} path must be relative")
    if ".." in raw.parts:
        raise SPEFSemanticError(f"{context} path escapes its artifact root via parent traversal")
    candidate = root / raw
    _reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SPEFSemanticError(f"{context} path escapes its artifact root") from exc
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise SPEFSemanticError(f"{context} is not a non-empty regular file: {resolved}")
    return resolved


def _verify_file(
    root: Path,
    relative: Any,
    expected_digest: Any,
    *,
    context: str,
) -> Path:
    path = _resolve_under(root, relative, context=context)
    expected = _require_digest(expected_digest, context=f"{context} digest")
    actual = _sha256(path)
    if actual != expected:
        raise SPEFSemanticError(f"{context} digest mismatch: expected {expected}, got {actual}")
    return path


def _copy_bound(source: Path, destination: Path, *, expected_digest: str, context: str) -> None:
    before = _sha256(source)
    if before != expected_digest:
        raise SPEFSemanticError(
            f"{context} changed before copy: expected {expected_digest}, got {before}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    after = _sha256(destination)
    if after != expected_digest:
        raise SPEFSemanticError(
            f"{context} changed while copying: expected {expected_digest}, got {after}"
        )


def _execution_context(source_revision: str | None) -> dict[str, Any]:
    revision = source_revision or os.environ.get("GITHUB_SHA")
    if revision is not None and _GIT_SHA_RE.fullmatch(revision) is None:
        raise SPEFSemanticError("source revision must be a lowercase 40-character Git SHA")
    upstream_run_id = os.environ.get("HEPHAESTUS_UPSTREAM_PHYSICAL_RUN_ID")
    if upstream_run_id is not None and (not upstream_run_id.isdigit() or int(upstream_run_id) <= 0):
        raise SPEFSemanticError("upstream physical workflow run ID must be a positive integer")
    return {
        "source_revision": revision,
        "upstream_physical_workflow_run_id": upstream_run_id,
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
    }
