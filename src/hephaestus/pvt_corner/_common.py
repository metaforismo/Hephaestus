"""Shared validation, path-safety, and provenance helpers for PVT evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
CORNERS = ("slow", "typ", "fast")
PHYSICAL_ATTEMPTS = (1, 2)
ANALYSIS_REPLAYS = (1, 2)
SCHEMA = "hephaestus.ihp-pvt-corner-evidence.v2"
EVIDENCE_LEVEL = "routed_spef_opensta_three_corner_characterization"
CONTRACT_SCHEMA = "hephaestus.ihp-pvt-corner-contract.v2"
CONTRACT_ID = "ihp-sg13g2-routed-pvt-corner-v2"
REFERENCE_SCHEMA = "hephaestus.ihp-pvt-corner-reference.v2"
REFERENCE_ID = "ihp-sg13g2-routed-pvt-corner-tiny-v2"


class PVTCornerError(RuntimeError):
    """Raised when PVT evidence cannot preserve its declared boundary."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PVTCornerError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PVTCornerError(f"JSON artifact must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def require_string(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise PVTCornerError(f"{context} must be a non-empty string")
    return value


def require_sha256(value: Any, *, context: str) -> str:
    text = require_string(value, context=context)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PVTCornerError(f"{context} must be a lowercase SHA-256 digest")
    return text


def require_git_sha(value: Any, *, context: str) -> str:
    text = require_string(value, context=context)
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
        raise PVTCornerError(f"{context} must be a lowercase 40-character Git SHA")
    return text


def require_positive_int(value: Any, *, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise PVTCornerError(f"{context} must be a positive integer")
    return value


def require_finite_number(value: Any, *, context: str) -> float:
    if type(value) not in (int, float):
        raise PVTCornerError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PVTCornerError(f"{context} must be finite")
    return result


def require_claims(
    claims: Any,
    *,
    required_true: tuple[str, ...],
    required_false: tuple[str, ...],
    context: str,
) -> dict[str, Any]:
    if not isinstance(claims, dict):
        raise PVTCornerError(f"{context} claims are malformed")
    missing_true = [name for name in required_true if claims.get(name) is not True]
    missing_false = [name for name in required_false if claims.get(name) is not False]
    if missing_true or missing_false:
        raise PVTCornerError(
            f"{context} claim boundary is invalid: true={missing_true}, false={missing_false}"
        )
    return claims


def absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def reject_symlink_components(candidate: Path, *, context: str) -> None:
    absolute = absolute_without_symlink_resolution(candidate)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise PVTCornerError(f"{context} path must not contain symlinks: {absolute}")


def resolve_input_directory(path: Path, *, context: str) -> Path:
    candidate = absolute_without_symlink_resolution(path)
    reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise PVTCornerError(f"{context} is not a directory: {resolved}")
    return resolved


def resolve_input_file(path: Path, *, context: str) -> Path:
    candidate = absolute_without_symlink_resolution(path)
    reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise PVTCornerError(f"{context} is not a non-empty regular file: {resolved}")
    return resolved


def resolve_output_directory(path: Path) -> Path:
    candidate = absolute_without_symlink_resolution(path)
    reject_symlink_components(candidate.parent, context="output directory parent")
    resolved = candidate.resolve()
    if resolved.exists():
        raise PVTCornerError(
            f"output directory already exists; refusing destructive replacement: {resolved}"
        )
    return resolved


def paths_overlap(lhs: Path, rhs: Path) -> bool:
    return lhs == rhs or lhs in rhs.parents or rhs in lhs.parents


def resolve_under(root: Path, relative: Any, *, context: str) -> Path:
    raw_text = require_string(relative, context=f"{context} path")
    raw = Path(raw_text)
    if raw.is_absolute():
        raise PVTCornerError(f"{context} path must be relative")
    if ".." in raw.parts:
        raise PVTCornerError(f"{context} path escapes its artifact root via parent traversal")
    candidate = root / raw
    reject_symlink_components(candidate, context=context)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PVTCornerError(f"{context} path escapes its artifact root") from exc
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise PVTCornerError(f"{context} is not a non-empty regular file: {resolved}")
    return resolved


def verify_file(
    root: Path,
    relative: Any,
    expected_digest: Any,
    *,
    context: str,
    expected_size: Any | None = None,
) -> Path:
    path = resolve_under(root, relative, context=context)
    expected = require_sha256(expected_digest, context=f"{context} digest")
    actual = sha256_file(path)
    if actual != expected:
        raise PVTCornerError(f"{context} digest mismatch: expected {expected}, got {actual}")
    if expected_size is not None:
        size = require_positive_int(expected_size, context=f"{context} size")
        if path.stat().st_size != size:
            raise PVTCornerError(
                f"{context} size mismatch: expected {size}, got {path.stat().st_size}"
            )
    return path


def copy_bound(
    source: Path,
    destination: Path,
    *,
    expected_digest: str,
    context: str,
) -> None:
    before = sha256_file(source)
    if before != expected_digest:
        raise PVTCornerError(
            f"{context} changed before copy: expected {expected_digest}, got {before}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    after = sha256_file(destination)
    if after != expected_digest:
        raise PVTCornerError(
            f"{context} changed while copying: expected {expected_digest}, got {after}"
        )


def execution_context(
    source_revision: str,
    *,
    upstream_run_id: str | None = None,
) -> dict[str, Any]:
    revision = require_git_sha(source_revision, context="source revision")
    run_id = upstream_run_id or os.environ.get("HEPHAESTUS_UPSTREAM_PHYSICAL_RUN_ID")
    if run_id is not None and (not run_id.isdigit() or int(run_id) <= 0):
        raise PVTCornerError("upstream physical workflow run ID must be a positive integer")
    return {
        "source_revision": revision,
        "upstream_physical_workflow_run_id": run_id,
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
    }
