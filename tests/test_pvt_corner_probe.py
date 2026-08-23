from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "flows" / "openroad" / "pvt_corner" / "run_probe.py"
SPEC = importlib.util.spec_from_file_location("pvt_corner_probe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_corner_discovery_selects_expected_files(tmp_path: Path) -> None:
    files = {
        "sg13g2_stdcell_slow_1p08V_125C.lib": "slow",
        "sg13g2_stdcell_typ_1p20V_25C.lib": "typ",
        "sg13g2_stdcell_fast_1p32V_m40C.lib": "fast",
    }
    for name in files:
        (tmp_path / name).write_text("library(test) {}\n", encoding="utf-8")

    selected = probe.discover_liberty_corners(tmp_path)

    assert {label: path.name for label, path in selected.items()} == {
        value: name for name, value in files.items()
    }


def test_corner_discovery_rejects_ambiguous_best_match(tmp_path: Path) -> None:
    for prefix in ("a", "b"):
        (tmp_path / f"{prefix}_sg13g2_stdcell_typ_1p20V_25C.lib").write_text(
            "library(test) {}\n",
            encoding="utf-8",
        )
    (tmp_path / "sg13g2_stdcell_slow_1p08V_125C.lib").write_text(
        "library(test) {}\n",
        encoding="utf-8",
    )
    (tmp_path / "sg13g2_stdcell_fast_1p32V_m40C.lib").write_text(
        "library(test) {}\n",
        encoding="utf-8",
    )

    with pytest.raises(probe.PVTProbeError, match="ambiguous"):
        probe.discover_liberty_corners(tmp_path)


def test_tighten_sdc_replaces_only_first_clock_period() -> None:
    source = "\n".join(
        [
            "create_clock -name core -period 4.0 [get_ports clk]",
            "set_input_delay 0.2 -clock core [all_inputs -no_clocks]",
            "create_clock -name virtual -period 4.0",
            "",
        ]
    )

    tightened = probe.tighten_sdc(source, 0.05)

    assert "create_clock -name core -period 0.05" in tightened
    assert "create_clock -name virtual -period 4.0" in tightened


def test_tighten_sdc_rejects_missing_clock() -> None:
    with pytest.raises(probe.PVTProbeError, match="replaceable create_clock"):
        probe.tighten_sdc("set_load 0.01 [all_outputs]\n", 0.05)


def test_parse_opensta_metrics_accepts_met_and_violated() -> None:
    met = probe.parse_opensta_metrics(
        "  0.643739 slack (MET)\ntns 0.000000\nHEPHAESTUS_PVT_DONE=1\n"
    )
    violated = probe.parse_opensta_metrics(
        " -0.125 slack (VIOLATED)\ntns -1.25\nHEPHAESTUS_PVT_DONE=1\n"
    )

    assert met == {
        "worst_setup_slack_ns": 0.643739,
        "slack_status": "met",
        "total_negative_slack_ns": 0.0,
    }
    assert violated == {
        "worst_setup_slack_ns": -0.125,
        "slack_status": "violated",
        "total_negative_slack_ns": -1.25,
    }


def test_parse_opensta_metrics_requires_completion_marker() -> None:
    with pytest.raises(probe.PVTProbeError, match="completion marker"):
        probe.parse_opensta_metrics("0.1 slack (MET)\ntns 0.0\n")


def test_metrics_equal_is_strict_but_allows_identical_none() -> None:
    left = {
        "worst_setup_slack_ns": 0.25,
        "slack_status": "met",
        "total_negative_slack_ns": None,
    }
    assert probe.metrics_equal(left, dict(left))
    changed = dict(left)
    changed["worst_setup_slack_ns"] = 0.250001
    assert not probe.metrics_equal(left, changed)


def test_resolve_by_digest_rejects_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    with pytest.raises(probe.PVTProbeError, match="unsafe"):
        probe.resolve_by_digest(
            root,
            root,
            {
                "path": "../outside.v",
                "sha256": probe.sha256_file(outside),
            },
            context="fixture",
        )


def test_resolve_by_digest_uses_unique_digest_match(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    artifact = nested / "6_final.v"
    artifact.write_text("module routed; endmodule\n", encoding="utf-8")

    resolved = probe.resolve_by_digest(
        root,
        root,
        {
            "path": "stale-runner-path/6_final.v",
            "sha256": probe.sha256_file(artifact),
        },
        context="fixture",
    )

    assert resolved == artifact.resolve()


def test_physical_prerequisite_rejects_missing_repeatability(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    prepared_dir = tmp_path / "prepared"
    evidence_dir.mkdir()
    prepared_dir.mkdir()
    prepared = {
        "schema": "hephaestus.openroad-physical-prepared.v1",
        "backends": {name: {} for name in probe.BACKENDS},
    }
    prepared_path = prepared_dir / "prepared.json"
    prepared_path.write_text(json.dumps(prepared) + "\n", encoding="utf-8")
    claims = {
        "registered_source_binding_verified": True,
        "pinned_orfs_image_used": True,
        "all_three_backends_placed": True,
        "all_three_backends_routed": True,
        "all_three_backends_emitted_spef": True,
        "two_attempts_per_backend_completed": True,
        "physical_repeatability_verified": False,
        "common_physical_boundary_verified": True,
    }
    evidence = {
        "schema": "hephaestus.openroad-physical-evidence.v1",
        "backends": {name: {} for name in probe.BACKENDS},
        "source": {
            "prepared_manifest_sha256": probe.sha256_file(prepared_path),
        },
        "claims": claims,
    }
    (evidence_dir / "openroad_physical_evidence.json").write_text(
        json.dumps(evidence) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(probe.PVTProbeError, match="prerequisite"):
        probe.validate_physical_evidence(tmp_path)


def test_emit_opensta_script_binds_all_inputs(tmp_path: Path) -> None:
    artifacts = {}
    for name in ("corner.lib", "final.v", "final.sdc", "final.spef"):
        path = tmp_path / name
        path.write_text("fixture\n", encoding="utf-8")
        artifacts[name] = path

    script = probe.emit_opensta_script(
        liberty=artifacts["corner.lib"],
        netlist=artifacts["final.v"],
        top="registered_tile",
        sdc=artifacts["final.sdc"],
        spef=artifacts["final.spef"],
        label="typ",
    )

    assert "read_liberty" in script
    assert "read_verilog" in script
    assert "link_design registered_tile" in script
    assert "read_sdc" in script
    assert "read_spef" in script
    assert "report_worst_slack -max" in script
    assert "HEPHAESTUS_PVT_DONE=1" in script


def test_run_opensta_records_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "opensta"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(
            cmd=["opensta"],
            timeout=1,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(probe.subprocess, "run", timeout)

    with pytest.raises(probe.PVTProbeError, match="timed out"):
        probe.run_opensta(
            opensta=executable,
            workdir=tmp_path / "run",
            script="exit\n",
            attempt=1,
            timeout=1,
        )
    assert (tmp_path / "run" / "attempt-1.stdout.txt").read_text(
        encoding="utf-8"
    ) == "partial stdout"


def test_sha256_file_is_content_bound(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(struct.pack(">III", 1, 2, 3))
    first = probe.sha256_file(path)
    path.write_bytes(struct.pack(">III", 1, 2, 4))
    assert probe.sha256_file(path) != first
