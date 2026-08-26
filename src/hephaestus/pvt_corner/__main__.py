"""Command-line entry point for routed PVT evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    PVTCornerError,
    build_evidence,
    build_reference,
    validate_existing_reference,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hephaestus.pvt_corner",
        description="Generate or validate exact-head routed IHP PVT evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("physical_root", type=Path)
    run.add_argument("post_physical_root", type=Path)
    run.add_argument("--pdk", type=Path, required=True)
    run.add_argument("--opensta", type=Path, required=True)
    run.add_argument("--opensta-manifest", type=Path, required=True)
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--reference", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--source-revision", required=True)
    run.add_argument("--upstream-run-id")

    reference = subparsers.add_parser("reference")
    reference.add_argument("evidence", type=Path)
    reference.add_argument("--out", type=Path, required=True)

    validate = subparsers.add_parser("validate-reference")
    validate.add_argument("evidence", type=Path)
    validate.add_argument("--reference", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "run":
            result = build_evidence(
                args.physical_root,
                args.post_physical_root,
                args.pdk,
                args.opensta,
                args.opensta_manifest,
                args.contract,
                args.out,
                source_revision=args.source_revision,
                reference_path=args.reference,
                upstream_run_id=args.upstream_run_id,
            )
        elif args.command == "reference":
            result = build_reference(args.evidence, args.out)
        else:
            result = validate_existing_reference(args.evidence, args.reference)
    except (PVTCornerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
