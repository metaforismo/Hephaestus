"""Command-line entry point for permanent post-physical equivalence evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import PostPhysicalEquivalenceError, build_evidence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prove exact registered source implementations against routed netlists."
    )
    parser.add_argument("physical_root", type=Path)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--source-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evidence = build_evidence(
            args.physical_root,
            args.models,
            args.reference,
            args.out,
            yosys=args.yosys,
            timeout=args.timeout,
            source_revision=args.source_revision,
        )
    except (PostPhysicalEquivalenceError, ValueError) as exc:
        print(f"post-physical equivalence failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema": evidence["schema"],
                "reference": evidence["regression"]["reference_id"],
                "claims": evidence["claims"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
