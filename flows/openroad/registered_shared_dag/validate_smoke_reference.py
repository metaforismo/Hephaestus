#!/usr/bin/env python3
"""Validate a routed registered-tile smoke run against a pinned reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"[0-9a-f]{64}")
SECTION_COUNT_RE = re.compile(
    r"^(?P<section>COMPONENTS|NETS|SPECIALNETS|PINS|VIAS)\s+(?P<count>\d+)\s*;"
)
UNITS_RE = re.compile(r"^UNITS DISTANCE MICRONS (?P<value>\d+)\s*;")
DIE_RE = re.compile(
    r"^DIEAREA \( (?P<x0>-?\d+) (?P<y0>-?\d+) \) "
    r"\( (?P<x1>-?\d+) (?P<y1>-?\d+) \)\s*;"
)
DATE_RE = re.compile(r'^\*DATE ".*"$', re.MULTILINE)


class SmokeValidationError(RuntimeError):
    """Raised when the smoke run differs from its pinned reference."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeValidationError(f"JSON root must be an object: {path}")
    return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise SmokeValidationError(f"{context} is not a lowercase SHA-256 digest")
    return value


def exactly_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise SmokeValidationError(
            f"expected exactly one {pattern!r}, found {len(matches)}: {matches}"
        )
    path = matches[0]
    if not path.is_file() or path.stat().st_size == 0:
        raise SmokeValidationError(f"required artifact is missing or empty: {path}")
    if path.is_symlink():
        raise SmokeValidationError(f"required artifact must not be a symlink: {path}")
    return path


def parse_def(path: Path) -> dict[str, Any]:
    units: int | None = None
    die_area_dbu: list[int] | None = None
    counts: dict[str, int] = {}
    row_count = 0
    track_count = 0

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("ROW "):
            row_count += 1
        elif line.startswith("TRACKS "):
            track_count += 1

        units_match = UNITS_RE.fullmatch(line)
        if units_match is not None:
            units = int(units_match.group("value"))
            continue

        die_match = DIE_RE.fullmatch(line)
        if die_match is not None:
            die_area_dbu = [
                int(die_match.group("x0")),
                int(die_match.group("y0")),
                int(die_match.group("x1")),
                int(die_match.group("y1")),
            ]
            continue

        section_match = SECTION_COUNT_RE.fullmatch(line)
        if section_match is not None:
            counts[section_match.group("section")] = int(section_match.group("count"))

    if units is None or units <= 0:
        raise SmokeValidationError("DEF does not contain valid database units")
    if die_area_dbu is None:
        raise SmokeValidationError("DEF does not contain one DIEAREA statement")
    required_sections = {"COMPONENTS", "NETS", "SPECIALNETS", "PINS", "VIAS"}
    if set(counts) != required_sections:
        raise SmokeValidationError(
            f"DEF section counts are incomplete: expected {required_sections}, got {set(counts)}"
        )

    return {
        "database_units_per_micron": units,
        "die_area_dbu": die_area_dbu,
        "die_area_um": [value / units for value in die_area_dbu],
        "component_count": counts["COMPONENTS"],
        "net_count": counts["NETS"],
        "special_net_count": counts["SPECIALNETS"],
        "pin_count": counts["PINS"],
        "via_definition_count": counts["VIAS"],
        "row_statement_count": row_count,
        "track_statement_count": track_count,
    }


def normalize_gds_timestamps(raw: bytes) -> tuple[bytes, int]:
    output = bytearray(raw)
    offset = 0
    normalized = 0

    while offset < len(raw):
        if offset + 4 > len(raw):
            raise SmokeValidationError("truncated GDS record header")
        record_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        if record_length < 4 or record_length % 2 != 0:
            raise SmokeValidationError(
                f"invalid GDS record length {record_length} at offset {offset}"
            )
        end = offset + record_length
        if end > len(raw):
            raise SmokeValidationError("truncated GDS record payload")
        record_type = raw[offset + 2]
        if record_type in (0x01, 0x05):
            if record_length != 28:
                raise SmokeValidationError(f"unexpected timestamp record length {record_length}")
            output[offset + 4 : end] = b"\x00" * (record_length - 4)
            normalized += 1
        offset = end

    if offset != len(raw):
        raise SmokeValidationError("GDS parser did not consume the complete file")
    if normalized == 0:
        raise SmokeValidationError("GDS contains no BGNLIB/BGNSTR timestamps")
    return bytes(output), normalized


def normalize_spef_date(raw: bytes) -> tuple[bytes, int]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeValidationError("SPEF is not valid UTF-8") from exc
    matches = DATE_RE.findall(text)
    if len(matches) != 1:
        raise SmokeValidationError(f"expected one SPEF *DATE record, found {len(matches)}")
    normalized = DATE_RE.sub('*DATE "<normalized>"', text, count=1)
    return normalized.encode("utf-8"), 1


def require_exact_artifact(
    path: Path,
    expected: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    expected_size = expected.get("size_bytes")
    if type(expected_size) is not int or expected_size <= 0:
        raise SmokeValidationError(f"{context}.size_bytes is invalid")
    expected_digest = require_sha256(expected.get("sha256"), context=context)
    actual = {
        "path": path.as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if actual["size_bytes"] != expected_size:
        raise SmokeValidationError(
            f"{context} size changed: expected {expected_size}, got {actual['size_bytes']}"
        )
    if actual["sha256"] != expected_digest:
        raise SmokeValidationError(
            f"{context} digest changed: expected {expected_digest}, got {actual['sha256']}"
        )
    return actual


def compare_metrics(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if set(actual) != set(expected):
        raise SmokeValidationError(
            f"metric key set changed: expected {sorted(expected)}, got {sorted(actual)}"
        )
    for name, expected_value in expected.items():
        actual_value = actual[name]
        if type(expected_value) not in (int, float):
            raise SmokeValidationError(f"reference metric {name} is not numeric")
        if type(actual_value) not in (int, float):
            raise SmokeValidationError(f"observed metric {name} is not numeric")
        if not math.isclose(
            float(actual_value),
            float(expected_value),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise SmokeValidationError(
                f"metric {name} changed: expected {expected_value}, got {actual_value}"
            )


def validate(
    root: Path,
    reference_path: Path,
    registered_root: Path,
    image_ref: str,
) -> dict[str, Any]:
    reference = load_json(reference_path)
    if reference.get("schema") != "hephaestus.openroad-registered-smoke-reference.v1":
        raise SmokeValidationError("unsupported smoke-reference schema")
    if reference.get("reference_id") != ("ihp-sg13g2-openroad-registered-shared-dag-smoke-v1"):
        raise SmokeValidationError("unexpected smoke-reference identity")

    expected_image = reference.get("toolchain", {}).get("orfs_image_repo_digest")
    if image_ref != expected_image:
        raise SmokeValidationError(
            f"workflow image differs from reference: {image_ref!r} != {expected_image!r}"
        )

    return_code_path = root / "orfs.returncode.txt"
    if return_code_path.read_text(encoding="utf-8").strip() != "0":
        raise SmokeValidationError("ORFS return code is not zero")

    registered_manifest_path = registered_root / "registered_manifest.json"
    registered_manifest = load_json(registered_manifest_path)
    shared = registered_manifest.get("backends", {}).get("shared_dag")
    if not isinstance(shared, dict):
        raise SmokeValidationError("registered manifest lacks shared_dag")
    source_reference = reference.get("source")
    if not isinstance(source_reference, dict):
        raise SmokeValidationError("reference source binding is malformed")

    source_observed = {
        "registered_manifest_sha256": sha256_file(registered_manifest_path),
        "core_sha256": sha256_file(registered_root / str(shared.get("core_rtl"))),
        "wrapper_sha256": sha256_file(registered_root / str(shared.get("wrapper_rtl"))),
    }
    for field, observed in source_observed.items():
        expected = require_sha256(source_reference.get(field), context=f"source.{field}")
        if observed != expected:
            raise SmokeValidationError(
                f"source binding {field} changed: expected {expected}, got {observed}"
            )

    repo_digests_path = root / "provenance/orfs-image-repodigests.json"
    repo_digests = json.loads(repo_digests_path.read_text(encoding="utf-8"))
    if not isinstance(repo_digests, list) or expected_image not in repo_digests:
        raise SmokeValidationError(
            f"pulled image does not expose the pinned RepoDigest: {repo_digests!r}"
        )
    image_id = (root / "provenance/orfs-image-id.txt").read_text(encoding="utf-8").strip()
    expected_image_id = reference.get("toolchain", {}).get("orfs_image_id")
    if image_id != expected_image_id:
        raise SmokeValidationError(
            f"ORFS image ID changed: expected {expected_image_id}, got {image_id}"
        )

    tool_versions_path = root / "provenance/tool-versions.txt"
    tool_versions = tool_versions_path.read_text(encoding="utf-8")
    expected_versions = reference.get("toolchain", {}).get("tool_versions")
    if not isinstance(expected_versions, dict):
        raise SmokeValidationError("reference tool versions are malformed")
    required_fragments = (
        str(expected_versions.get("openroad")),
        str(expected_versions.get("yosys")),
        str(expected_versions.get("klayout")),
    )
    for fragment in required_fragments:
        if fragment not in tool_versions:
            raise SmokeValidationError(f"tool-version banner does not contain {fragment!r}")

    compatibility_path = exactly_one(
        root,
        "results/**/smoke/1_2_yosys.opensta_compat.json",
    )
    compatibility = load_json(compatibility_path)
    compatibility_reference = reference.get("compatibility_transform")
    if not isinstance(compatibility_reference, dict):
        raise SmokeValidationError("reference compatibility transform is malformed")
    if compatibility.get("schema") != compatibility_reference.get("schema"):
        raise SmokeValidationError("compatibility-transform schema changed")
    if compatibility.get("substitution_count") != compatibility_reference.get("substitution_count"):
        raise SmokeValidationError("compatibility substitution count changed")
    if compatibility.get("original") != compatibility_reference.get("original_netlist"):
        raise SmokeValidationError("original synthesized-netlist signature changed")
    if compatibility.get("sanitized") != compatibility_reference.get("sanitized_netlist"):
        raise SmokeValidationError("sanitized synthesized-netlist signature changed")
    if sha256_file(compatibility_path) != compatibility_reference.get("manifest_sha256"):
        raise SmokeValidationError("compatibility manifest digest changed")

    outputs = {
        "final_gds": exactly_one(root, "results/**/smoke/6_final.gds"),
        "final_def": exactly_one(root, "results/**/smoke/6_final.def"),
        "final_open_db": exactly_one(root, "results/**/smoke/6_final.odb"),
        "final_verilog": exactly_one(root, "results/**/smoke/6_final.v"),
        "final_spef": exactly_one(root, "results/**/smoke/6_final.spef"),
        "final_sdc": exactly_one(root, "results/**/smoke/6_final.sdc"),
        "route_guide": exactly_one(root, "results/**/smoke/route.guide"),
    }
    artifact_reference = reference.get("artifacts")
    if not isinstance(artifact_reference, dict):
        raise SmokeValidationError("reference artifacts are malformed")

    exact_output_names = ("final_def", "final_verilog", "final_sdc", "route_guide")
    observed_outputs: dict[str, dict[str, Any]] = {}
    for name in exact_output_names:
        expected = artifact_reference.get(name)
        if not isinstance(expected, dict):
            raise SmokeValidationError(f"reference artifact {name} is malformed")
        observed_outputs[name] = require_exact_artifact(
            outputs[name],
            expected,
            context=f"artifacts.{name}",
        )

    for name in ("final_gds", "final_open_db", "final_spef"):
        path = outputs[name]
        observed_outputs[name] = {
            "path": path.as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    stable_reference = reference.get("stable_signatures")
    if not isinstance(stable_reference, dict):
        raise SmokeValidationError("reference stable signatures are malformed")
    observed_def = parse_def(outputs["final_def"])
    if observed_def != stable_reference.get("def"):
        raise SmokeValidationError(
            "parsed DEF signature changed: "
            f"expected {stable_reference.get('def')}, got {observed_def}"
        )

    normalized_gds, gds_count = normalize_gds_timestamps(outputs["final_gds"].read_bytes())
    normalized_gds_digest = sha256_bytes(normalized_gds)
    if normalized_gds_digest != stable_reference.get("gds_timestamp_normalized_sha256"):
        raise SmokeValidationError("timestamp-normalized GDS signature changed")
    if gds_count != stable_reference.get("gds_timestamp_records_normalized"):
        raise SmokeValidationError("GDS timestamp-record count changed")

    normalized_spef, spef_count = normalize_spef_date(outputs["final_spef"].read_bytes())
    normalized_spef_digest = sha256_bytes(normalized_spef)
    if normalized_spef_digest != stable_reference.get("spef_date_normalized_sha256"):
        raise SmokeValidationError("date-normalized SPEF signature changed")
    if spef_count != stable_reference.get("spef_date_records_normalized"):
        raise SmokeValidationError("SPEF date-record count changed")

    report_path = exactly_one(root, "logs/**/smoke/6_report.json")
    report = load_json(report_path)
    expected_metrics = reference.get("metrics")
    if not isinstance(expected_metrics, dict):
        raise SmokeValidationError("reference metrics are malformed")
    actual_metrics = {name: report.get(name) for name in expected_metrics}
    compare_metrics(actual_metrics, expected_metrics)

    claims = reference.get("claims")
    if not isinstance(claims, dict):
        raise SmokeValidationError("reference claims are malformed")
    required_true = (
        "registered_source_binding_verified",
        "single_backend_orfs_flow_completed",
        "placement_performed",
        "routing_performed",
        "gds_generated",
        "spef_generated",
        "timing_constraints_analyzed",
        "four_nanosecond_target_met_in_qualifying_run",
    )
    required_false = (
        "matched_three_backend_physical_comparison_performed",
        "post_physical_equivalence_verified",
        "drc_clean",
        "lvs_clean",
        "power_estimated_with_activity",
        "post_layout_pex_verified",
        "foundry_signoff_complete",
        "silicon_verified",
    )
    if any(claims.get(name) is not True for name in required_true):
        raise SmokeValidationError("reference lacks a required positive claim")
    if any(claims.get(name) is not False for name in required_false):
        raise SmokeValidationError("reference overstates its claim boundary")

    return {
        "schema": "hephaestus.openroad-registered-smoke-validation.v1",
        "reference_id": reference["reference_id"],
        "reference_sha256": sha256_file(reference_path),
        "source": source_observed,
        "toolchain": {
            "orfs_image_repo_digest": image_ref,
            "orfs_image_id": image_id,
            "tool_versions_sha256": sha256_file(tool_versions_path),
        },
        "compatibility_transform": {
            "manifest_sha256": sha256_file(compatibility_path),
            "substitution_count": compatibility["substitution_count"],
        },
        "outputs": observed_outputs,
        "stable_signatures": {
            "def": observed_def,
            "gds_timestamp_normalized_sha256": normalized_gds_digest,
            "gds_timestamp_records_normalized": gds_count,
            "spef_date_normalized_sha256": normalized_spef_digest,
            "spef_date_records_normalized": spef_count,
        },
        "metrics": actual_metrics,
        "claims": claims,
    }


def self_test() -> None:
    spef = b'*SPEF "ieee 1481-1999"\n*DATE "today"\n*DESIGN "x"\n'
    normalized_spef, count = normalize_spef_date(spef)
    assert count == 1
    assert b"today" not in normalized_spef

    timestamp_payload = b"\x00" * 24
    gds = (
        struct.pack(">HBB", 28, 0x01, 0x02) + timestamp_payload + struct.pack(">HBB", 4, 0x04, 0x00)
    )
    normalized_gds, count = normalize_gds_timestamps(gds)
    assert count == 1
    assert len(normalized_gds) == len(gds)

    try:
        normalize_spef_date(b'*SPEF "x"\n')
    except SmokeValidationError:
        pass
    else:
        raise AssertionError("SPEF without *DATE was accepted")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--registered", type=Path)
    parser.add_argument("--image-ref")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        print("OpenROAD smoke-reference validator self-test passed.")
        return 0
    required = (args.root, args.reference, args.registered, args.image_ref, args.out)
    if any(value is None for value in required):
        raise SystemExit("--root, --reference, --registered, --image-ref, and --out are required")
    result = validate(
        args.root,
        args.reference,
        args.registered,
        args.image_ref,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
