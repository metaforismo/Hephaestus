"""Shared validation and provenance helpers for post-physical equivalence."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_FAULTS = ("data", "valid", "reset")
_SCHEMA = "hephaestus.post-physical-equivalence-evidence.v1"
_REFERENCE_SCHEMA = "hephaestus.post-physical-equivalence-reference.v1"
_REFERENCE_ID = "ihp-sg13g2-post-physical-equivalence-tiny-v1"
_EVIDENCE_LEVEL = "exact_registered_source_to_routed_sequential_equivalence"
_EQUIV_SEQUENCE_LENGTH = 4
_BASE_CASE_CYCLES = 5
_BASE_CASE_PROVE_SKIP = 1
_RESET_SEQUENCE = (1, 0, 0, 0, 0)
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SUCCESS_MARKER = "Equivalence successfully proven!"
_SAT_SUCCESS_MARKER = "SAT proof finished - no model found: SUCCESS!"
_SAT_FAILURE_MARKER = "SAT proof finished - model found: FAIL!"
_STATUS_RE = re.compile(
    r"Found (?P<total>\d+) \$equiv cells in equiv:\s*"
    r"Of those cells (?P<proven>\d+) are proven and "
    r"(?P<unproven>\d+) are unproven\."
)
_NEGATIVE_STATUS_RE = re.compile(
    r"ERROR: Found (?P<unproven>\d+) unproven \$equiv cells "
    r"in 'equiv_status -assert'\."
)
_INDUCTION_STEP_RE = re.compile(r"Proving induction step (?P<step>\d+)\.")
_YOSYS_VERSION_RE = re.compile(r"^Yosys \S+ \(git sha1 [0-9a-f]+\)$")


class PostPhysicalEquivalenceError(RuntimeError):
    """Raised when permanent post-physical evidence cannot be built safely."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostPhysicalEquivalenceError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PostPhysicalEquivalenceError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PostPhysicalEquivalenceError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: Any, *, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise PostPhysicalEquivalenceError(f"{context} must be a positive integer")
    return value


def _safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise PostPhysicalEquivalenceError(f"{context} is not a safe module name: {value!r}")
    return value


def _reject_symlink_components(root: Path, candidate: Path, *, context: str) -> None:
    current = candidate
    while current != root:
        if current.is_symlink():
            raise PostPhysicalEquivalenceError(
                f"{context} path must not contain symlinks: {candidate}"
            )
        parent = current.parent
        if parent == current:
            raise PostPhysicalEquivalenceError(f"{context} path has no bounded parent")
        current = parent


def _resolve_under(root: Path, relative: Any, *, context: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise PostPhysicalEquivalenceError(f"{context} path must be a non-empty string")
    raw = Path(relative)
    if raw.is_absolute():
        raise PostPhysicalEquivalenceError(f"{context} path must be relative")
    if ".." in raw.parts:
        raise PostPhysicalEquivalenceError(f"{context} path must not contain parent traversal")
    resolved_root = root.resolve()
    candidate = resolved_root / raw
    _reject_symlink_components(resolved_root, candidate, context=context)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PostPhysicalEquivalenceError(f"{context} path escapes its artifact root") from exc
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise PostPhysicalEquivalenceError(f"{context} is not a non-empty regular file: {resolved}")
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
        raise PostPhysicalEquivalenceError(
            f"{context} digest mismatch: expected {expected}, got {actual}"
        )
    return path


def _copy_bound(source: Path, destination: Path, *, expected_digest: str, context: str) -> None:
    actual = _sha256(source)
    if actual != expected_digest:
        raise PostPhysicalEquivalenceError(
            f"{context} changed before copy: expected {expected_digest}, got {actual}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    copied = _sha256(destination)
    if copied != expected_digest:
        raise PostPhysicalEquivalenceError(
            f"{context} changed while copying: expected {expected_digest}, got {copied}"
        )


def _require_claims(
    claims: Any,
    *,
    required_true: tuple[str, ...],
    required_false: tuple[str, ...],
    context: str,
) -> dict[str, Any]:
    if not isinstance(claims, dict):
        raise PostPhysicalEquivalenceError(f"{context} claims are malformed")
    missing_true = [name for name in required_true if claims.get(name) is not True]
    missing_false = [name for name in required_false if claims.get(name) is not False]
    if missing_true or missing_false:
        raise PostPhysicalEquivalenceError(
            f"{context} claim boundary is invalid: true={missing_true}, false={missing_false}"
        )
    return claims
