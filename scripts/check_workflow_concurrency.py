#!/usr/bin/env python3
"""Require every permanent workflow to cancel stale ref runs."""

from __future__ import annotations

import sys
from pathlib import Path

EXPECTED = (
    "concurrency:\n"
    '  group: "${{ github.workflow }}-'
    '${{ github.event.pull_request.number || github.ref }}"\n'
    "  cancel-in-progress: true"
)


def violations(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted((root / ".github/workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if EXPECTED not in text:
            failures.append(f"workflow lacks bounded concurrency: {path}")
        if "      - feat/" in text:
            failures.append(f"workflow retains a stale feature push branch: {path}")
    return failures


def main() -> int:
    failures = violations(Path.cwd())
    if failures:
        print("Workflow concurrency check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Workflow concurrency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
