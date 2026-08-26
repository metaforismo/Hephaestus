#!/usr/bin/env python3
"""Independently inspect an IHP KLayout DRC research artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
ATTEMPTS = (1, 2)
PDK_COMMIT = "22f2a25f1734796de3debbbf29cf697cbbc54081"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class InspectionError(RuntimeError):
    """Raised when the research artifact cannot satisfy its narrow contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InspectionError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InspectionError(f"JSON artifact must be an object: {path}")
    return value


def safe_root(path: Path) -> Path:
    raw = Path(os.path.abspath(os.fspath(path)))
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        if current.is_symlink():
            raise InspectionError(f"artifact root contains a symlink: {raw}")
    root = raw.resolve()
    if not root.is_dir():
        raise InspectionError(f"artifact root is not a directory: {root}")
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise InspectionError(f"artifact contains a symlink: {candidate}")
    return root


def resolve_under(root: Path, relative: Any, *, context: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise InspectionError(f"{context} path must be a non-empty string")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise InspectionError(f"{context} path is unsafe: {relative!r}")
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise InspectionError(f"{context} path escapes the artifact root") from exc
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise InspectionError(f"{context} is not a non-empty regular file: {path}")
    return path


def parse_lyrdb(path: Path) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise InspectionError(f"cannot parse KLayout report database {path}: {exc}") from exc
    items = root.findall(".//item")
    categories = root.findall(".//category")
    cells = root.findall(".//cell")
    item_categories: dict[str, int] = {}
    for item in items:
        category = item.findtext("category") or item.get("category") or "<unknown>"
        item_categories[category] = item_categories.get(category, 0) + 1
    return {
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "root_tag": root.tag,
        "item_count": len(items),
        "category_count": len(categories),
        "cell_count": len(cells),
        "item_categories": dict(sorted(item_categories.items())),
    }


def require_summary_equal(recorded: Any, replayed: dict[str, Any], *, context: str) -> None:
    if not isinstance(recorded, dict):
        raise InspectionError(f"recorded report summary is malformed: {context}")
    for name in (
        "sha256",
        "size_bytes",
        "root_tag",
        "item_count",
        "category_count",
        "cell_count",
        "item_categories",
    ):
        if recorded.get(name) != replayed.get(name):
            raise InspectionError(
                f"report replay differs for {context}/{name}: "
                f"recorded={recorded.get(name)!r}, replayed={replayed.get(name)!r}"
            )


def git_value(root: Path, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise InspectionError(f"cannot read Git metadata below {root}: {args}")
    return value


def inspect(
    artifact_root: Path,
    pdk_root: Path,
    *,
    expected_source_revision: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_revision) is None:
        raise InspectionError("expected source revision must be a 40-character Git SHA")
    root = safe_root(artifact_root)
    result_files = list(root.rglob("research-result.json"))
    if len(result_files) != 1:
        raise InspectionError(
            f"expected one research-result.json, found {len(result_files)}"
        )
    result_path = result_files[0]
    evidence_root = result_path.parent
    result = load_object(result_path)
    if result.get("schema") != "hephaestus.research.ihp-klayout-drc-probe.v1":
        raise InspectionError("unsupported DRC research schema")
    execution = result.get("execution")
    if not isinstance(execution, dict):
        raise InspectionError("DRC execution provenance is malformed")
    if execution.get("source_revision") != expected_source_revision:
        raise InspectionError("DRC research artifact was produced from another source revision")
    upstream = execution.get("upstream_physical_workflow_run_id")
    if not isinstance(upstream, str) or not upstream.isdigit() or int(upstream) <= 0:
        raise InspectionError("DRC artifact lacks a positive upstream workflow run ID")

    pdk_root = pdk_root.resolve()
    if git_value(pdk_root, "rev-parse", "HEAD") != PDK_COMMIT:
        raise InspectionError("IHP Open PDK checkout differs from the pinned commit")
    toolchain = result.get("toolchain")
    if not isinstance(toolchain, dict):
        raise InspectionError("DRC toolchain provenance is malformed")
    if toolchain.get("ihp_open_pdk_commit") != PDK_COMMIT:
        raise InspectionError("recorded IHP Open PDK commit differs")
    selected_deck = toolchain.get("selected_deck")
    if not isinstance(selected_deck, str):
        raise InspectionError("selected DRC deck path is missing")
    deck = resolve_under(pdk_root, selected_deck, context="selected DRC deck")
    expected_deck_digest = toolchain.get("selected_deck_sha256")
    if (
        not isinstance(expected_deck_digest, str)
        or _SHA256_RE.fullmatch(expected_deck_digest) is None
        or sha256_file(deck) != expected_deck_digest
    ):
        raise InspectionError("selected DRC deck digest differs from the pinned PDK")

    inventory = load_object(
        resolve_under(evidence_root, "pdk-inventory.json", context="PDK inventory")
    )
    matching = [
        value
        for value in inventory.get("decks", [])
        if isinstance(value, dict) and value.get("path") == selected_deck
    ]
    if len(matching) != 1 or matching[0].get("sha256") != expected_deck_digest:
        raise InspectionError("selected deck is not uniquely represented in the inventory")

    matrix = result.get("matrix")
    expected_labels = {
        f"{backend}-attempt-{attempt}"
        for backend in BACKENDS
        for attempt in ATTEMPTS
    }
    if not isinstance(matrix, dict) or set(matrix) != expected_labels:
        raise InspectionError("DRC research matrix differs from six matched cases")

    positive_counts: dict[str, int] = {}
    negative_counts: dict[str, int] = {}
    positive_empty = True
    controls_strictly_increased = True
    source_bindings = 0
    for label in sorted(expected_labels):
        case = matrix[label]
        if not isinstance(case, dict):
            raise InspectionError(f"DRC case is malformed: {label}")
        source = case.get("source")
        if not isinstance(source, dict):
            raise InspectionError(f"source binding is malformed: {label}")
        source_digest = source.get("gds_sha256")
        if not isinstance(source_digest, str) or _SHA256_RE.fullmatch(source_digest) is None:
            raise InspectionError(f"source GDS digest is malformed: {label}")
        case_root = evidence_root / "cases" / case["backend"] / f"attempt-{case['attempt']}"
        positive_gds = resolve_under(
            case_root,
            "positive.gds",
            context=f"{label} positive GDS",
        )
        if sha256_file(positive_gds) != source_digest:
            raise InspectionError(f"staged positive GDS differs from its source: {label}")
        source_bindings += 1

        mutation = case.get("mutation")
        if not isinstance(mutation, dict):
            raise InspectionError(f"mutation provenance is malformed: {label}")
        negative_gds = resolve_under(
            case_root,
            "negative.gds",
            context=f"{label} negative GDS",
        )
        if sha256_file(negative_gds) != mutation.get("mutated_sha256"):
            raise InspectionError(f"negative GDS digest differs: {label}")
        if mutation.get("source_sha256") != source_digest:
            raise InspectionError(f"mutation source digest differs: {label}")
        inserted = mutation.get("metadata", {}).get("inserted")
        if not isinstance(inserted, list) or len(inserted) < 1:
            raise InspectionError(f"negative geometry metadata is empty: {label}")
        source_bindings += 1

        positive_record = case.get("positive")
        negative_record = case.get("negative")
        if not isinstance(positive_record, dict) or not isinstance(negative_record, dict):
            raise InspectionError(f"DRC report records are malformed: {label}")
        positive_report = resolve_under(
            evidence_root,
            positive_record.get("report"),
            context=f"{label} positive report",
        )
        negative_report = resolve_under(
            evidence_root,
            negative_record.get("report"),
            context=f"{label} negative report",
        )
        replayed_positive = parse_lyrdb(positive_report)
        replayed_negative = parse_lyrdb(negative_report)
        require_summary_equal(
            positive_record.get("summary"),
            replayed_positive,
            context=f"{label} positive",
        )
        require_summary_equal(
            negative_record.get("summary"),
            replayed_negative,
            context=f"{label} negative",
        )
        positive_counts[label] = replayed_positive["item_count"]
        negative_counts[label] = replayed_negative["item_count"]
        positive_empty = positive_empty and replayed_positive["item_count"] == 0
        strict_increase = (
            replayed_negative["item_count"] > replayed_positive["item_count"]
        )
        controls_strictly_increased = controls_strictly_increased and strict_increase
        if negative_record.get("detected") is not True:
            raise InspectionError(f"negative control was not recorded as detected: {label}")

    claims = result.get("claims")
    if not isinstance(claims, dict):
        raise InspectionError("research DRC claims are malformed")
    for name in (
        "research_probe_completed",
        "official_ihp_open_drc_deck_executed",
        "all_six_exact_routed_gds_files_bound",
        "all_six_positive_report_databases_parsed",
        "all_six_geometry_controls_executed",
        "geometry_corruption_detected",
    ):
        if claims.get(name) is not True:
            raise InspectionError(f"required research claim is not true: {name}")
    for name in (
        "open_minimal_drc_qualified",
        "drc_clean",
        "foundry_signoff_drc_clean",
        "foundry_signoff_complete",
        "silicon_verified",
    ):
        if claims.get(name) is not False:
            raise InspectionError(f"research claim must remain false: {name}")
    if claims.get("all_six_positive_reports_empty") is not positive_empty:
        raise InspectionError("recorded positive-report emptiness differs from replay")
    if not controls_strictly_increased:
        raise InspectionError(
            "one or more geometry controls did not strictly increase report item count"
        )

    deck_name = Path(selected_deck).name.lower()
    deck_class = (
        "maximal"
        if "maximal" in deck_name or "maximum" in deck_name
        else "minimal"
        if "minimal" in deck_name
        else "other-official-open-deck"
    )
    return {
        "schema": "hephaestus.research.ihp-klayout-drc-inspection.v1",
        "source_revision": expected_source_revision,
        "upstream_physical_workflow_run_id": int(upstream),
        "pdk_commit": PDK_COMMIT,
        "selected_deck": selected_deck,
        "selected_deck_sha256": expected_deck_digest,
        "selected_deck_class": deck_class,
        "positive_reports_replayed": 6,
        "negative_reports_replayed": 6,
        "source_gds_bindings_rechecked": source_bindings,
        "positive_item_counts": positive_counts,
        "negative_item_counts": negative_counts,
        "all_positive_reports_empty": positive_empty,
        "all_controls_strictly_increased_item_count": controls_strictly_increased,
        "methodology_ready_for_permanent_candidate": (
            positive_empty and controls_strictly_increased
        ),
        "claim_boundary": {
            "open_minimal_drc_qualified": False,
            "drc_clean": False,
            "foundry_signoff_drc_clean": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
        "result": "passed",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("artifact", type=Path)
    value.add_argument("--pdk", type=Path, required=True)
    value.add_argument("--expected-source-revision", required=True)
    value.add_argument("--out", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.out.exists() or args.out.is_symlink():
        raise InspectionError(f"inspection output already exists or is a symlink: {args.out}")
    report = inspect(
        args.artifact,
        args.pdk,
        expected_source_revision=args.expected_source_revision,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
