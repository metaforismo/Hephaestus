#!/usr/bin/env python3
"""Research wrapper that fixes OpenSTA repeatability comparison semantics."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import opensta_sdc_probe_base as base

_COMPARISON_FIELDS = (
    "returncode",
    "period_ns",
    "worst_slack_ns",
    "total_negative_slack_ns",
    "derived_data_delay_ns",
    "stdout_sha256",
    "stderr_sha256",
)


def _signature(result: dict[str, Any]) -> dict[str, Any]:
    return {field: result[field] for field in _COMPARISON_FIELDS}


def run_probe(
    prepared_dir: Path,
    sta_path: Path,
    tool_metadata_path: Path,
    *,
    attempts: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run OpenSTA and compare report contents rather than attempt filenames."""

    if attempts < 2:
        raise ValueError("attempts must be at least two")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    root = prepared_dir.resolve()
    sta = sta_path.resolve()
    if not sta.is_file() or not sta.stat().st_mode & 0o111:
        raise base.ProbeError(f"OpenSTA binary is missing or not executable: {sta}")
    tool_metadata = base._load_json(tool_metadata_path.resolve())
    if not isinstance(tool_metadata, dict):
        raise base.ProbeError("tool metadata must be a JSON object")
    if tool_metadata.get("binary_sha256") != base._sha256(sta):
        raise base.ProbeError("OpenSTA binary digest differs from tool metadata")

    prepared = base._load_json(root / "prepared.json")
    if (
        not isinstance(prepared, dict)
        or prepared.get("schema") != "hephaestus.opensta-sdc-prepared.v1"
    ):
        raise base.ProbeError("prepared OpenSTA manifest is missing or unsupported")
    analyses = prepared.get("analyses")
    if not isinstance(analyses, list) or not analyses:
        raise base.ProbeError("prepared OpenSTA manifest contains no analyses")

    normalized: list[dict[str, Any]] = []
    for item in analyses:
        if not isinstance(item, dict) or not isinstance(item.get("directory"), str):
            raise base.ProbeError("prepared OpenSTA analysis entry is malformed")
        relative = Path(item["directory"])
        if relative.is_absolute():
            raise base.ProbeError("prepared OpenSTA analysis path must be relative")
        run_dir = (root / relative).resolve()
        try:
            run_dir.relative_to(root)
        except ValueError as exc:
            raise base.ProbeError("prepared OpenSTA analysis path escapes its root") from exc
        metadata = base._load_json(run_dir / "metadata.json")
        if not isinstance(metadata, dict):
            raise base.ProbeError(f"metadata is malformed for {run_dir.name}")

        attempt_results = [
            base._run_once(
                sta,
                run_dir,
                attempt=attempt,
                timeout_seconds=timeout_seconds,
            )
            for attempt in range(1, attempts + 1)
        ]
        first_signature = _signature(attempt_results[0])
        if any(_signature(result) != first_signature for result in attempt_results[1:]):
            raise base.ProbeError(
                f"OpenSTA report content is not byte-identical for {run_dir.name}"
            )

        normalized.append(
            {
                **metadata,
                "timing": first_signature,
                "attempt_artifacts": [
                    {
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                    }
                    for result in attempt_results
                ],
                "attempts": attempts,
                "repeatability_passed": True,
            }
        )

    mapped_digests = {result["mapped_verilog_sha256"] for result in normalized}
    if len(mapped_digests) != len(normalized):
        raise base.ProbeError("normalized OpenSTA results do not cover distinct mapped netlists")

    summary = {
        "schema": "hephaestus.opensta-sdc-probe.v1",
        "evidence_level": "opensta_sdc_pre_layout_timing_probe",
        "tool": tool_metadata,
        "source": prepared.get("source"),
        "contract": prepared.get("contract"),
        "assumptions": prepared.get("assumptions"),
        "results": normalized,
        "claims": {
            "opensta_binary_built_from_pinned_source": True,
            "sdc_constraints_applied": True,
            "setup_checks_passed": True,
            "detailed_max_path_reported": True,
            "pre_layout_timing_analyzed": True,
            "repeatability_verified": True,
            "signoff_sta_performed": False,
            "timing_closed": False,
            "parasitics_annotated": False,
            "placement_performed": False,
            "routing_performed": False,
            "power_estimated": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    base._write_json(root / "opensta_sdc_probe.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    arguments = base.build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = base.prepare_probe(
                arguments.area_delay_bundle,
                arguments.out,
                period_ns=arguments.period_ns,
                input_delay_ns=arguments.input_delay_ns,
                output_delay_ns=arguments.output_delay_ns,
                driving_cell=arguments.driving_cell,
                output_load_pf=arguments.output_load_pf,
                labels=tuple(arguments.labels),
            )
            print(f"prepared {len(result['analyses'])} OpenSTA analyses")
        else:
            result = run_probe(
                arguments.prepared_dir,
                arguments.sta,
                arguments.tool_metadata,
                attempts=arguments.attempts,
                timeout_seconds=arguments.timeout,
            )
            print(f"verified {len(result['results'])} repeatable OpenSTA timing analyses")
    except (base.ProbeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
