#!/usr/bin/env python3
"""Fail when generated or restricted artifacts are tracked by Git."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

BLOCKED_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
BLOCKED_SUFFIXES = {
    ".gds",
    ".gdsii",
    ".lef",
    ".lib",
    ".oas",
    ".oasis",
    ".pyc",
    ".spef",
    ".vcd",
}
ALLOWED_LIBERTY_METADATA = {
    PurePosixPath("configs/technology/ihp_sg13g2_stdcell_typ_1p20V_25C.json"),
}


def tracked_files() -> list[PurePosixPath]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [PurePosixPath(raw.decode("utf-8")) for raw in completed.stdout.split(b"\0") if raw]


def violations(paths: list[PurePosixPath]) -> list[str]:
    failures: list[str] = []
    for path in paths:
        if any(part in BLOCKED_PARTS for part in path.parts):
            failures.append(f"generated directory is tracked: {path}")
            continue
        suffix = path.suffix.lower()
        if suffix in BLOCKED_SUFFIXES and path not in ALLOWED_LIBERTY_METADATA:
            failures.append(f"generated or PDK artifact is tracked: {path}")
    return sorted(failures)


def required_ignore_entries(root: Path) -> list[str]:
    ignore = root / ".gitignore"
    if not ignore.is_file():
        return [".gitignore is missing"]
    active = {
        line.strip()
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    groups = {
        "build output": {"build/", "/build/", "build"},
        "Python bytecode": {"__pycache__/", "__pycache__", "*.py[cod]", "*.pyc"},
        "virtual environments": {".venv/", ".venv", "venv/", "venv"},
        "pytest cache": {".pytest_cache/", ".pytest_cache"},
        "Ruff cache": {".ruff_cache/", ".ruff_cache"},
    }
    return [
        f".gitignore does not cover {label}"
        for label, alternatives in groups.items()
        if active.isdisjoint(alternatives)
    ]


def main() -> int:
    failures = violations(tracked_files())
    failures.extend(required_ignore_entries(Path.cwd()))
    if failures:
        print("Repository hygiene check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
