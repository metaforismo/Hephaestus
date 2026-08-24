from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hephaestus import pvt_corner


def _contract() -> dict[str, object]:
    return {
        "schema": "hephaestus.ihp-pvt-corner-contract.v1",
        "contract_id": "ihp-sg13g2-routed-pvt-corner-v1",
        "backends": list(pvt_corner._BACKENDS),
        "corner_order": list(pvt_corner._CORNER_LABELS),
        "attempts_per_corner": 2,
        "timeout_seconds": 600,
        "negative_control_clock_period_ns": 0.05,
        "ihp_open_pdk": {
            "repository": "https://example.invalid/pdk.git",
            "commit": "1" * 40,
        },
        "corner_selectors": {
            "slow": {"required_filename_tokens": ["slow", "1p08", "125"]},
            "typ": {"required_filename_tokens": ["typ", "1p20", "25"]},
            "fast": {"required_filename_tokens": ["fast", "1p32", "m40"]},
        },
        "claim_boundary": {
            "ocv_aocv_pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }


def test_contract_validation_accepts_exact_boundary(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(_contract()) + "\n", encoding="utf-8")

    assert pvt_corner._validate_contract(path) == _contract()


def test_contract_validation_rejects_signoff_claim(tmp_path: Path) -> None:
    value = _contract()
    claim_boundary = dict(value["claim_boundary"])
    claim_boundary["foundry_signoff_complete"] = True
    value["claim_boundary"] = claim_boundary
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(pvt_corner.PVTCornerError, match="overstates"):
        pvt_corner._validate_contract(path)


def test_corner_discovery_uses_contract_tokens(tmp_path: Path) -> None:
    for name in (
        "sg13g2_stdcell_slow_1p08V_125C.lib",
        "sg13g2_stdcell_typ_1p20V_25C.lib",
        "sg13g2_stdcell_fast_1p32V_m40C.lib",
    ):
        (tmp_path / name).write_text("library(test) {}\n", encoding="utf-8")

    selected = pvt_corner._discover_liberty_corners(tmp_path, _contract())

    assert selected["slow"].name.endswith("slow_1p08V_125C.lib")
    assert selected["typ"].name.endswith("typ_1p20V_25C.lib")
    assert selected["fast"].name.endswith("fast_1p32V_m40C.lib")


def test_corner_discovery_rejects_ambiguity(tmp_path: Path) -> None:
    for prefix in ("a", "b"):
        (tmp_path / f"{prefix}_typ_1p20_25.lib").write_text(
            "library(test) {}\n",
            encoding="utf-8",
        )
    (tmp_path / "slow_1p08_125.lib").write_text(
        "library(test) {}\n", encoding="utf-8"
    )
    (tmp_path / "fast_1p32_m40.lib").write_text(
        "library(test) {}\n", encoding="utf-8"
    )

    with pytest.raises(pvt_corner.PVTCornerError, match="expected one"):
        pvt_corner._discover_liberty_corners(tmp_path, _contract())


def test_resolve_by_digest_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    with pytest.raises(pvt_corner.PVTCornerError, match="unsafe"):
        pvt_corner._resolve_by_digest(
            root,
            root,
            {
                "path": "../outside.v",
                "sha256": pvt_corner.sha256_file(outside),
            },
            context="fixture",
        )


def test_resolve_by_digest_uses_unique_match(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    artifact = nested / "6_final.v"
    artifact.write_text("module routed; endmodule\n", encoding="utf-8")

    resolved = pvt_corner._resolve_by_digest(
        root,
        root,
        {
            "path": "old-runner/6_final.v",
            "sha256": pvt_corner.sha256_file(artifact),
        },
        context="fixture",
    )

    assert resolved == artifact.resolve()


def test_tighten_sdc_changes_only_first_clock() -> None:
    source = "\n".join(
        [
            "create_clock -name core -period 4.0 [get_ports clk]",
            "create_clock -name virtual -period 4.0",
            "",
        ]
    )

    tightened = pvt_corner._tighten_sdc(source, 0.05)

    assert "create_clock -name core -period 0.05" in tightened
    assert "create_clock -name virtual -period 4.0" in tightened


def test_parse_metrics_requires_completion_marker() -> None:
    with pytest.raises(pvt_corner.PVTCornerError, match="completion marker"):
        pvt_corner._parse_metrics("0.1 slack (MET)\ntns 0.0\n")


def test_parse_metrics_accepts_violation() -> None:
    assert pvt_corner._parse_metrics(
        "-0.125 slack (VIOLATED)\ntns -1.25\nHEPHAESTUS_PVT_DONE=1\n"
    ) == {
        "worst_setup_slack_ns": -0.125,
        "slack_status": "violated",
        "total_negative_slack_ns": -1.25,
    }


def test_metrics_repeatability_is_exact_to_declared_tolerance() -> None:
    value = {
        "worst_setup_slack_ns": 0.25,
        "slack_status": "met",
        "total_negative_slack_ns": None,
    }
    assert pvt_corner._metrics_equal(value, dict(value))
    changed = dict(value)
    changed["worst_setup_slack_ns"] = 0.250001
    assert not pvt_corner._metrics_equal(value, changed)


def test_run_opensta_preserves_timeout_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(pvt_corner.subprocess, "run", timeout)

    with pytest.raises(pvt_corner.PVTCornerError, match="timed out"):
        pvt_corner._run_opensta(
            executable=executable,
            workdir=tmp_path / "run",
            script="exit\n",
            attempt=1,
            timeout=1,
        )
    assert (tmp_path / "run" / "attempt-1.stdout.txt").read_text(
        encoding="utf-8"
    ) == "partial stdout"


def _qualified_evidence() -> dict[str, object]:
    corners = {
        label: {
            "liberty_sha256": str(index + 1) * 64,
            "metrics": {
                "worst_setup_slack_ns": 0.1 * (index + 1),
                "slack_status": "met",
                "total_negative_slack_ns": 0.0,
            },
        }
        for index, label in enumerate(pvt_corner._CORNER_LABELS)
    }
    backends = {
        backend: {
            "top_module": f"top_{backend}",
            "routed_verilog": {"sha256": "a" * 64},
            "routed_spef": {"sha256": "b" * 64},
            "sdc": {"sha256": "c" * 64},
            "corners": corners,
            "negative_control": {
                "clock_period_ns": 0.05,
                "timing_violation_observed": True,
            },
        }
        for backend in pvt_corner._BACKENDS
    }
    return {
        "schema": "hephaestus.ihp-pvt-corner-evidence.v1",
        "toolchain": {"ihp_open_pdk_commit": "d" * 40},
        "corner_order": list(pvt_corner._CORNER_LABELS),
        "backends": backends,
        "claims": {
            "comparative_pvt_claim_enabled": True,
            "ocv_aocv_pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }


def test_reference_round_trip(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    reference_path = tmp_path / "reference.json"
    evidence_path.write_text(
        json.dumps(_qualified_evidence()) + "\n",
        encoding="utf-8",
    )

    pvt_corner.build_reference(evidence_path, reference_path)
    result = pvt_corner.validate_reference(evidence_path, reference_path)

    assert result["passed"] is True
    assert result["evidence_sha256"] == pvt_corner.sha256_file(evidence_path)


def test_reference_validation_rejects_metric_drift(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    reference_path = tmp_path / "reference.json"
    evidence = _qualified_evidence()
    evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    pvt_corner.build_reference(evidence_path, reference_path)
    evidence["backends"]["shared_dag"]["corners"]["slow"]["metrics"][
        "worst_setup_slack_ns"
    ] = -1.0
    evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")

    with pytest.raises(pvt_corner.PVTCornerError, match="regression changed"):
        pvt_corner.validate_reference(evidence_path, reference_path)
