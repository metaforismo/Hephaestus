from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.opensta_binding import OpenSTABindingError, build_opensta_formal_binding


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _positive_proof(digest: str) -> dict[str, object]:
    return {
        "mapped_verilog_sha256": digest,
        "proof": {
            "performed": True,
            "passed": True,
            "proof_success": True,
            "counterexample_found": False,
            "unsupported_cell_error": False,
        },
    }


def _fixtures(tmp_path: Path) -> tuple[Path, Path]:
    backends = ("shared_dag", "naive_shift_add", "constant_multipliers")
    formal_backends: dict[str, object] = {}
    timing_results: list[dict[str, object]] = []
    for index, backend in enumerate(backends):
        digest = f"{index + 1:064x}"
        formal_backends[backend] = {
            "all_pareto_runs_covered": True,
            "pareto_labels": ["unconstrained"],
            "runs": {
                "unconstrained": {
                    "mapped_verilog_sha256": digest,
                    "proof_representative": "unconstrained",
                    "equivalence_verified": True,
                }
            },
            "proofs": {"unconstrained": _positive_proof(digest)},
        }
        timing_results.append(
            {
                "backend": backend,
                "label": "unconstrained",
                "mapped_verilog_sha256": digest,
                "repeatability_passed": True,
                "attempts": 2,
                "abc_library_area": 10.0 + index,
                "abc_delay_picoseconds": 100.0 + index,
                "timing": {
                    "returncode": 0,
                    "period_ns": 4.0,
                    "derived_data_delay_ns": 2.0,
                    "worst_slack_ns": 2.0,
                },
            }
        )

    formal = {
        "schema": "hephaestus.abc-area-delay-formal-evidence.v1",
        "evidence_level": "yosys_sat_abc_area_delay_mapped_equivalence",
        "backends": formal_backends,
        "technology": {"technology_id": "test"},
        "negative_control": {
            "proof": {
                "performed": True,
                "passed": True,
                "proof_success": False,
                "counterexample_found": True,
                "unsupported_cell_error": False,
            }
        },
        "claims": {
            "abc_area_delay_source_evidence_verified": True,
            "all_abc_sweep_mapped_netlists_equivalent": True,
            "all_pareto_mapped_netlists_equivalent": True,
            "mapped_gate_level_equivalence_verified": True,
            "exhaustive_combinational_equivalence_verified": True,
            "negative_control_counterexample_found": True,
        },
    }
    timing = {
        "schema": "hephaestus.opensta-sdc-probe.v1",
        "evidence_level": "opensta_sdc_pre_layout_timing_probe",
        "assumptions": {"virtual_clock_period_ns": 4.0},
        "tool": {"commit": "a" * 40},
        "results": timing_results,
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
    formal_path = tmp_path / "formal.json"
    timing_path = tmp_path / "timing.json"
    _write(formal_path, formal)
    _write(timing_path, timing)
    return formal_path, timing_path


def test_binding_requires_exact_formal_digests(tmp_path: Path) -> None:
    formal_path, timing_path = _fixtures(tmp_path)
    output = tmp_path / "binding.json"

    result = build_opensta_formal_binding(formal_path, timing_path, output)

    assert result["scope"]["formally_proved_timed_netlists"] == 3
    assert result["scope"]["unique_mapped_verilog_digests"] == 3
    assert result["claims"]["all_timed_netlists_formally_equivalent"]
    assert result["claims"]["signoff_sta_performed"] is False
    assert output.is_file()


def test_binding_rejects_a_timing_digest_mismatch(tmp_path: Path) -> None:
    formal_path, timing_path = _fixtures(tmp_path)
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    timing["results"][0]["mapped_verilog_sha256"] = "f" * 64
    _write(timing_path, timing)

    with pytest.raises(OpenSTABindingError, match="digest differs"):
        build_opensta_formal_binding(formal_path, timing_path, tmp_path / "out.json")


def test_binding_rejects_an_unproved_netlist(tmp_path: Path) -> None:
    formal_path, timing_path = _fixtures(tmp_path)
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    formal["backends"]["shared_dag"]["runs"]["unconstrained"]["equivalence_verified"] = False
    _write(formal_path, formal)

    with pytest.raises(OpenSTABindingError, match="was not proved"):
        build_opensta_formal_binding(formal_path, timing_path, tmp_path / "out.json")


def test_binding_requires_a_real_negative_control(tmp_path: Path) -> None:
    formal_path, timing_path = _fixtures(tmp_path)
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    formal["negative_control"]["proof"]["counterexample_found"] = False
    _write(formal_path, formal)

    with pytest.raises(OpenSTABindingError, match="negative control"):
        build_opensta_formal_binding(formal_path, timing_path, tmp_path / "out.json")


def test_binding_rejects_duplicate_mapped_digests(tmp_path: Path) -> None:
    formal_path, timing_path = _fixtures(tmp_path)
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    digest = formal["backends"]["shared_dag"]["runs"]["unconstrained"]["mapped_verilog_sha256"]
    formal["backends"]["naive_shift_add"]["runs"]["unconstrained"]["mapped_verilog_sha256"] = digest
    formal["backends"]["naive_shift_add"]["proofs"]["unconstrained"]["mapped_verilog_sha256"] = (
        digest
    )
    timing["results"][1]["mapped_verilog_sha256"] = digest
    _write(formal_path, formal)
    _write(timing_path, timing)

    with pytest.raises(OpenSTABindingError, match="unexpectedly reuse"):
        build_opensta_formal_binding(formal_path, timing_path, tmp_path / "out.json")
