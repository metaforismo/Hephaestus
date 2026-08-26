#!/usr/bin/env python3
"""Research-only probe for the official IHP SG13G2 KLayout DRC collateral.

This script deliberately does not promote a permanent DRC claim. It binds the
six exact routed GDS files from one matched physical artifact, discovers the
usable official ``.lydrc`` invocation, parses real KLayout report databases, and
checks that deterministic geometry corruption changes the report outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
ATTEMPTS = (1, 2)
SCHEMA = "hephaestus.research.ihp-klayout-drc-probe.v1"
PDK_COMMIT = "22f2a25f1734796de3debbbf29cf697cbbc54081"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ProbeError(RuntimeError):
    """Raised when the research probe cannot preserve its evidence boundary."""


@dataclass(frozen=True)
class PhysicalCase:
    backend: str
    attempt: int
    gds: Path
    gds_sha256: str
    gds_size_bytes: int
    run_manifest: Path
    run_manifest_sha256: str


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
        raise ProbeError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProbeError(f"JSON artifact must be an object: {path}")
    return value


def write_object(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProbeError(f"{context} must be a lowercase SHA-256 digest")
    return value


def resolve_under(root: Path, relative: Any, *, context: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ProbeError(f"{context} path must be a non-empty string")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ProbeError(f"{context} path is unsafe: {relative!r}")
    candidate = root / raw
    current = Path(candidate.absolute().anchor)
    for part in candidate.absolute().parts[1:]:
        current /= part
        if current.is_symlink():
            raise ProbeError(f"{context} path contains a symbolic link: {candidate}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ProbeError(f"{context} path escapes its artifact root") from exc
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ProbeError(f"{context} is not a non-empty regular file: {resolved}")
    return resolved


def bind_physical_cases(physical_root: Path) -> tuple[dict[str, Any], list[PhysicalCase]]:
    physical_root = physical_root.resolve()
    manifest_path = resolve_under(
        physical_root,
        "evidence/openroad_physical_evidence.json",
        context="physical evidence manifest",
    )
    evidence = load_object(manifest_path)
    if evidence.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise ProbeError("unsupported matched physical evidence schema")
    if evidence.get("evidence_level") != "matched_registered_orfs_rtl_to_gds_repeatability":
        raise ProbeError("unexpected matched physical evidence level")
    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        raise ProbeError("physical evidence claims are malformed")
    for name in (
        "registered_source_binding_verified",
        "pinned_orfs_image_used",
        "all_three_backends_placed",
        "all_three_backends_routed",
        "all_three_backends_emitted_gds",
        "two_attempts_per_backend_completed",
        "physical_repeatability_verified",
        "common_physical_boundary_verified",
    ):
        if claims.get(name) is not True:
            raise ProbeError(f"required physical prerequisite is not true: {name}")
    for name in (
        "drc_clean",
        "lvs_clean",
        "power_estimated_with_activity",
        "post_layout_pex_verified",
        "foundry_signoff_complete",
        "silicon_verified",
    ):
        if claims.get(name) is not False:
            raise ProbeError(f"physical prerequisite overstates downstream claim: {name}")

    backends = evidence.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(BACKENDS):
        raise ProbeError("physical backend set differs from the matched contract")
    cases: list[PhysicalCase] = []
    for backend in BACKENDS:
        value = backends[backend]
        if value.get("repeatability", {}).get("passed") is not True:
            raise ProbeError(f"physical repeatability is false for {backend}")
        runs = value.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise ProbeError(f"physical attempt set is malformed for {backend}")
        for attempt in ATTEMPTS:
            record = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            if record is None:
                raise ProbeError(f"missing physical attempt {backend}/{attempt}")
            manifest_digest = require_sha256(
                record.get("manifest_sha256"),
                context=f"{backend}/{attempt} run manifest",
            )
            bound_manifest = resolve_under(
                physical_root / "evidence",
                record.get("manifest"),
                context=f"{backend}/{attempt} bound run manifest",
            )
            if sha256_file(bound_manifest) != manifest_digest:
                raise ProbeError(f"bound run-manifest digest differs for {backend}/{attempt}")
            run_root = (
                physical_root
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            ).resolve()
            original_manifest = resolve_under(
                run_root,
                "openroad_run.json",
                context=f"{backend}/{attempt} original run manifest",
            )
            if sha256_file(original_manifest) != manifest_digest:
                raise ProbeError(f"original run-manifest digest differs for {backend}/{attempt}")
            run = load_object(original_manifest)
            if run.get("schema") != "hephaestus.openroad-physical-run.v1":
                raise ProbeError(f"unsupported physical run schema for {backend}/{attempt}")
            identity = run.get("identity")
            if not isinstance(identity, dict):
                raise ProbeError(f"physical identity is malformed for {backend}/{attempt}")
            if identity.get("backend") != backend or identity.get("attempt") != attempt:
                raise ProbeError(f"physical identity differs for {backend}/{attempt}")
            artifacts = run.get("artifacts")
            if not isinstance(artifacts, dict):
                raise ProbeError(f"physical artifacts are malformed for {backend}/{attempt}")
            gds_spec = artifacts.get("final_gds")
            if not isinstance(gds_spec, dict):
                raise ProbeError(f"final GDS metadata is missing for {backend}/{attempt}")
            gds_digest = require_sha256(
                gds_spec.get("sha256"),
                context=f"{backend}/{attempt} final GDS",
            )
            gds = resolve_under(
                run_root,
                gds_spec.get("path"),
                context=f"{backend}/{attempt} final GDS",
            )
            if sha256_file(gds) != gds_digest:
                raise ProbeError(f"final GDS digest differs for {backend}/{attempt}")
            size = gds_spec.get("size_bytes")
            if type(size) is not int or size <= 0 or gds.stat().st_size != size:
                raise ProbeError(f"final GDS size differs for {backend}/{attempt}")
            cases.append(
                PhysicalCase(
                    backend=backend,
                    attempt=attempt,
                    gds=gds,
                    gds_sha256=gds_digest,
                    gds_size_bytes=size,
                    run_manifest=original_manifest,
                    run_manifest_sha256=manifest_digest,
                )
            )
    if len(cases) != 6:
        raise ProbeError(f"expected six bound physical cases, got {len(cases)}")
    return evidence, cases


def pdk_inventory(pdk_root: Path) -> dict[str, Any]:
    pdk_root = pdk_root.resolve()
    completed = subprocess.run(
        ["git", "-C", str(pdk_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or commit != PDK_COMMIT:
        raise ProbeError(f"IHP Open PDK checkout differs: expected {PDK_COMMIT}, got {commit}")
    decks = sorted(pdk_root.rglob("*.lydrc"))
    if not decks:
        raise ProbeError("IHP Open PDK checkout contains no .lydrc files")
    layer_properties = sorted(pdk_root.rglob("*.lyp"))
    workflows = sorted((pdk_root / ".github" / "workflows").glob("*drc*"))
    return {
        "repository": "IHP-GmbH/IHP-Open-PDK",
        "commit": commit,
        "decks": [
            {
                "path": path.relative_to(pdk_root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in decks
        ],
        "layer_properties": [
            {
                "path": path.relative_to(pdk_root).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in layer_properties
        ],
        "drc_workflows": [
            {
                "path": path.relative_to(pdk_root).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in workflows
        ],
    }


def parse_lyrdb(path: Path) -> dict[str, Any]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise ProbeError(f"cannot parse KLayout report database {path}: {exc}") from exc
    root = tree.getroot()
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


def _new_reports(directory: Path, started_ns: int) -> list[Path]:
    reports = []
    for path in directory.rglob("*.lyrdb"):
        try:
            if path.stat().st_mtime_ns >= started_ns and path.stat().st_size > 0:
                reports.append(path.resolve())
        except OSError:
            continue
    return sorted(set(reports))


def _deck_preference(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    if "maximal" in name or "maximum" in name:
        return (0, path.as_posix())
    if "sg13g2" in name and "drc" in name and "minimal" not in name:
        return (1, path.as_posix())
    if "minimal" in name:
        return (2, path.as_posix())
    return (3, path.as_posix())


def invocation_candidates(gds: Path, report: Path) -> list[dict[str, str]]:
    pairs = (
        ("input", "report"),
        ("input", "report_file"),
        ("in_gds", "report"),
        ("in_gds", "report_file"),
        ("input_file", "report_file"),
        ("gds", "report"),
        ("filename", "report"),
    )
    values = []
    for input_key, report_key in pairs:
        values.append(
            {
                input_key: str(gds.resolve()),
                report_key: str(report.resolve()),
                "thr": "2",
            }
        )
    return values


def run_klayout_deck(
    *,
    klayout: Path,
    deck: Path,
    gds: Path,
    report: Path,
    variables: dict[str, str],
    workdir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=False)
    stdout = workdir / "stdout.txt"
    stderr = workdir / "stderr.txt"
    command = [str(klayout), "-b", "-r", str(deck.resolve())]
    for key, value in variables.items():
        command.extend(["-rd", f"{key}={value}"])
    started_ns = time.time_ns()
    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        stdout.write_text(out or "", encoding="utf-8")
        stderr.write_text(err or "", encoding="utf-8")
        return {
            "command": command,
            "returncode": "timeout",
            "stdout": stdout.name,
            "stdout_sha256": sha256_file(stdout),
            "stderr": stderr.name,
            "stderr_sha256": sha256_file(stderr),
            "reports": [],
        }
    stdout.write_text(completed.stdout, encoding="utf-8")
    stderr.write_text(completed.stderr, encoding="utf-8")
    candidates = []
    if report.is_file() and report.stat().st_size > 0:
        candidates.append(report.resolve())
    candidates.extend(_new_reports(workdir, started_ns))
    unique = sorted(set(candidates))
    parsed = []
    for candidate in unique:
        try:
            parsed.append({"path": str(candidate), "summary": parse_lyrdb(candidate)})
        except ProbeError as exc:
            parsed.append({"path": str(candidate), "parse_error": str(exc)})
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout.name,
        "stdout_sha256": sha256_file(stdout),
        "stderr": stderr.name,
        "stderr_sha256": sha256_file(stderr),
        "reports": parsed,
    }


def discover_invocation(
    *,
    klayout: Path,
    pdk_root: Path,
    inventory: dict[str, Any],
    gds: Path,
    output: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    attempts = []
    decks = [pdk_root / value["path"] for value in inventory["decks"]]
    for deck in sorted(decks, key=_deck_preference):
        for index, variables in enumerate(
            invocation_candidates(gds, output / "discovery-report.lyrdb"),
            start=1,
        ):
            report = output / f"candidate-{len(attempts) + 1:03d}.lyrdb"
            variables = {
                key: (str(report.resolve()) if key in {"report", "report_file"} else value)
                for key, value in variables.items()
            }
            record = run_klayout_deck(
                klayout=klayout,
                deck=deck,
                gds=gds,
                report=report,
                variables=variables,
                workdir=output / f"candidate-{len(attempts) + 1:03d}",
                timeout_seconds=timeout_seconds,
            )
            record.update(
                {
                    "deck": deck.relative_to(pdk_root).as_posix(),
                    "deck_sha256": sha256_file(deck),
                    "variables": variables,
                    "candidate_index": index,
                }
            )
            attempts.append(record)
            valid_reports = [
                value
                for value in record["reports"]
                if isinstance(value, dict) and "summary" in value
            ]
            if record["returncode"] == 0 and len(valid_reports) == 1:
                selected_report = Path(valid_reports[0]["path"])
                standardized = output / "selected-positive.lyrdb"
                shutil.copyfile(selected_report, standardized)
                selected_variables = {
                    key: ("{gds}" if value == str(gds.resolve()) else "{report}" if value == str(report.resolve()) else value)
                    for key, value in variables.items()
                }
                return {
                    "deck": deck.relative_to(pdk_root).as_posix(),
                    "deck_sha256": sha256_file(deck),
                    "variables": selected_variables,
                    "discovery_attempts": attempts,
                    "discovery_report": standardized.name,
                    "discovery_report_summary": parse_lyrdb(standardized),
                }
    write_object(output / "discovery-attempts.json", attempts)
    raise ProbeError("no official IHP .lydrc invocation produced one parseable report database")


def emit_mutator(path: Path) -> None:
    path.write_text(
        """import json
import pya

layout = pya.Layout()
layout.read(input)
tops = layout.top_cells()
if len(tops) != 1:
    raise RuntimeError(f"expected one top cell, got {len(tops)}")
top = tops[0]
counts = []
for layer_index in layout.layer_indexes():
    iterator = top.begin_shapes_rec(layer_index)
    count = 0
    while not iterator.at_end() and count < 20000:
        count += 1
        iterator.next()
    if count:
        info = layout.get_info(layer_index)
        counts.append((count, layer_index, info.layer, info.datatype))
counts.sort(reverse=True)
selected = counts[:12]
if not selected:
    raise RuntimeError("layout contains no populated drawing layers")
bbox = top.bbox()
step = max(1, int(round(0.25 / layout.dbu)))
size = 1
inserted = []
for rank, (count, layer_index, layer, datatype) in enumerate(selected):
    x = bbox.left + step * (rank + 2)
    y = bbox.bottom + step * (rank + 2)
    top.shapes(layer_index).insert(pya.Box(x, y, x + size, y + size))
    inserted.append({
        "rank": rank,
        "source_shape_count_capped": count,
        "layer_index": layer_index,
        "layer": layer,
        "datatype": datatype,
        "box_dbu": [x, y, x + size, y + size],
    })
layout.write(output)
with open(metadata, "w", encoding="utf-8") as handle:
    json.dump({"layout_dbu_um": layout.dbu, "inserted": inserted}, handle, indent=2, sort_keys=True)
    handle.write("\\n")
""",
        encoding="utf-8",
    )


def mutate_gds(
    *,
    klayout: Path,
    source: Path,
    destination: Path,
    metadata: Path,
    script: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        str(klayout),
        "-b",
        "-r",
        str(script.resolve()),
        "-rd",
        f"input={source.resolve()}",
        "-rd",
        f"output={destination.resolve()}",
        "-rd",
        f"metadata={metadata.resolve()}",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise ProbeError(
            "KLayout geometry mutation failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ProbeError("KLayout geometry mutation did not produce a GDS")
    if not metadata.is_file() or metadata.stat().st_size == 0:
        raise ProbeError("KLayout geometry mutation did not produce metadata")
    if sha256_file(destination) == sha256_file(source):
        raise ProbeError("geometry mutation left the GDS byte-identical")
    return {
        "command": command,
        "source_sha256": sha256_file(source),
        "mutated_sha256": sha256_file(destination),
        "mutated_size_bytes": destination.stat().st_size,
        "metadata": load_object(metadata),
    }


def instantiate_variables(template: dict[str, str], gds: Path, report: Path) -> dict[str, str]:
    return {
        key: value.replace("{gds}", str(gds.resolve())).replace(
            "{report}", str(report.resolve())
        )
        for key, value in template.items()
    }


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    physical_root = args.physical.resolve()
    pdk_root = args.pdk.resolve()
    output = args.out.resolve()
    if output.exists():
        raise ProbeError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    physical, cases = bind_physical_cases(physical_root)
    inventory = pdk_inventory(pdk_root)
    write_object(output / "pdk-inventory.json", inventory)
    version = subprocess.run(
        [str(args.klayout.resolve()), "-v"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version.returncode != 0:
        raise ProbeError("cannot obtain KLayout version")
    (output / "klayout-version.txt").write_text(
        version.stdout + version.stderr,
        encoding="utf-8",
    )

    discovery = discover_invocation(
        klayout=args.klayout.resolve(),
        pdk_root=pdk_root,
        inventory=inventory,
        gds=cases[0].gds,
        output=output / "discovery",
        timeout_seconds=args.timeout,
    )
    write_object(output / "selected-invocation.json", discovery)
    deck = pdk_root / discovery["deck"]
    mutator = output / "mutate_geometry.py"
    emit_mutator(mutator)

    results: dict[str, Any] = {}
    positive_empty = True
    negative_detected = True
    report_pairs = 0
    for case in cases:
        label = f"{case.backend}-attempt-{case.attempt}"
        case_root = output / "cases" / case.backend / f"attempt-{case.attempt}"
        case_root.mkdir(parents=True)
        copied_gds = case_root / "positive.gds"
        shutil.copyfile(case.gds, copied_gds)
        if sha256_file(copied_gds) != case.gds_sha256:
            raise ProbeError(f"GDS changed while staging {label}")
        positive_report = case_root / "positive.lyrdb"
        positive_record = run_klayout_deck(
            klayout=args.klayout.resolve(),
            deck=deck,
            gds=copied_gds,
            report=positive_report,
            variables=instantiate_variables(
                discovery["variables"],
                copied_gds,
                positive_report,
            ),
            workdir=case_root / "positive-run",
            timeout_seconds=args.timeout,
        )
        valid_positive = [value for value in positive_record["reports"] if "summary" in value]
        if positive_record["returncode"] != 0 or len(valid_positive) != 1:
            raise ProbeError(f"positive DRC did not produce one report for {label}")
        positive_source = Path(valid_positive[0]["path"])
        if positive_source != positive_report.resolve():
            shutil.copyfile(positive_source, positive_report)
        positive_summary = parse_lyrdb(positive_report)
        positive_empty = positive_empty and positive_summary["item_count"] == 0

        negative_gds = case_root / "negative.gds"
        mutation_metadata = case_root / "mutation.json"
        mutation = mutate_gds(
            klayout=args.klayout.resolve(),
            source=copied_gds,
            destination=negative_gds,
            metadata=mutation_metadata,
            script=mutator,
            timeout_seconds=args.timeout,
        )
        negative_report = case_root / "negative.lyrdb"
        negative_record = run_klayout_deck(
            klayout=args.klayout.resolve(),
            deck=deck,
            gds=negative_gds,
            report=negative_report,
            variables=instantiate_variables(
                discovery["variables"],
                negative_gds,
                negative_report,
            ),
            workdir=case_root / "negative-run",
            timeout_seconds=args.timeout,
        )
        valid_negative = [value for value in negative_record["reports"] if "summary" in value]
        if negative_record["returncode"] != 0 or len(valid_negative) != 1:
            raise ProbeError(f"negative DRC did not produce one report for {label}")
        negative_source = Path(valid_negative[0]["path"])
        if negative_source != negative_report.resolve():
            shutil.copyfile(negative_source, negative_report)
        negative_summary = parse_lyrdb(negative_report)
        detected = (
            negative_summary["item_count"] > positive_summary["item_count"]
            or negative_summary["sha256"] != positive_summary["sha256"]
        )
        negative_detected = negative_detected and detected
        report_pairs += 1
        results[label] = {
            "backend": case.backend,
            "attempt": case.attempt,
            "source": {
                "gds_sha256": case.gds_sha256,
                "gds_size_bytes": case.gds_size_bytes,
                "run_manifest_sha256": case.run_manifest_sha256,
            },
            "positive": {
                "record": positive_record,
                "report": positive_report.relative_to(output).as_posix(),
                "summary": positive_summary,
            },
            "mutation": mutation,
            "negative": {
                "record": negative_record,
                "report": negative_report.relative_to(output).as_posix(),
                "summary": negative_summary,
                "detected": detected,
            },
        }

    result = {
        "schema": SCHEMA,
        "execution": {
            "source_revision": args.source_revision,
            "upstream_physical_workflow_run_id": args.upstream_run_id,
        },
        "source": {
            "physical_evidence_sha256": sha256_file(
                physical_root / "evidence" / "openroad_physical_evidence.json"
            ),
        },
        "toolchain": {
            "ihp_open_pdk_commit": inventory["commit"],
            "selected_deck": discovery["deck"],
            "selected_deck_sha256": discovery["deck_sha256"],
            "klayout_version": (output / "klayout-version.txt").read_text(
                encoding="utf-8"
            ).strip(),
        },
        "matrix": results,
        "claims": {
            "research_probe_completed": True,
            "official_ihp_open_drc_deck_executed": True,
            "all_six_exact_routed_gds_files_bound": True,
            "all_six_positive_report_databases_parsed": report_pairs == 6,
            "all_six_geometry_controls_executed": report_pairs == 6,
            "geometry_corruption_detected": negative_detected,
            "all_six_positive_reports_empty": positive_empty,
            "open_minimal_drc_qualified": False,
            "drc_clean": False,
            "foundry_signoff_drc_clean": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_object(output / "research-result.json", result)
    return result


def self_test() -> None:
    root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "hephaestus-drc-self-test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    report = root / "tiny.lyrdb"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<report-database><categories><category><name>minimum width</name></category></categories>
<cells><cell><name>TOP</name></cell></cells><items><item><category>minimum width</category></item></items></report-database>
""",
        encoding="utf-8",
    )
    summary = parse_lyrdb(report)
    assert summary["item_count"] == 1
    assert summary["category_count"] == 1
    assert summary["cell_count"] == 1
    assert summary["item_categories"] == {"minimum width": 1}
    variables = invocation_candidates(Path("/tmp/in.gds"), Path("/tmp/out.lyrdb"))
    assert variables[0]["input"].endswith("/tmp/in.gds")
    assert variables[0]["report"].endswith("/tmp/out.lyrdb")
    shutil.rmtree(root)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    run = sub.add_parser("run")
    run.add_argument("--physical", type=Path, required=True)
    run.add_argument("--pdk", type=Path, required=True)
    run.add_argument("--klayout", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--source-revision", required=True)
    run.add_argument("--upstream-run-id", required=True)
    run.add_argument("--timeout", type=int, default=1200)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.command == "self-test":
        self_test()
        return 0
    if args.timeout <= 0 or args.timeout > 3600:
        raise ProbeError("timeout must be in the interval 1..3600 seconds")
    result = run_matrix(args)
    print(json.dumps(result["claims"], indent=2, sort_keys=True))
    if not result["claims"]["geometry_corruption_detected"]:
        raise ProbeError("geometry corruption was not detected across the full matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
