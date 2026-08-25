"""Command-line entry point for routed SPEF semantic evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import build_evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hephaestus.spef_semantic",
        description=(
            "Bind and semantically validate all six routed SPEF files from one "
            "qualified same-run physical/post-physical evidence chain."
        ),
    )
    parser.add_argument("physical_root", type=Path)
    parser.add_argument("post_physical_root", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-revision")
    return parser


def main() -> int:
    args = _parser().parse_args()
    build_evidence(
        args.physical_root,
        args.post_physical_root,
        args.reference,
        args.out,
        source_revision=args.source_revision,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
