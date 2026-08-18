from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.timing import (
    TimingEvidenceError,
    _build_script,
    _comparisons,
    _contains_value,
    _load_contract,
    _parse_output,
    _safe_relative,
)


def _contract() -> dict[str, object]:
    return {
        "schema": "hephaestus.combinational-timing-contract.v1",
        "contract_id": "test-contract",
        "technology_id": "test-tech",
        "virtual_clock_name": "TEST_CLOCK",
        "virtual_clock_period_ns": 100.0,
        "input_delay_ns": 0.0,
        "output_delay_ns": 0.0,
        "input_transition_ns": 0.1,
        "output_load_pf": 0.05,
        "path_delay": "max",
        "group_count": 5,
        "digits": 6,
    }


def test_contract_validation_and_script(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(_contract()), encoding="utf-8")
    contract = _load_contract(path)
    script = _build_script("safe_top", contract)

    assert "read_liberty ../../technology/technology.lib" in script
    assert "link_design safe_top" in script
    assert "create_clock -name TEST_CLOCK -period 100.0" in script
    assert "set_input_transition 0.1 [all_inputs]" in script
    assert "set_load 0.05 [all_outputs]" in script
    assert "report_checks -unconstrained" in script
    assert "HEPHAESTUS_BEGIN_MAX_PATHS" in script


def test_contract_rejects_unsafe_or_unsupported_values(tmp_path: Path) -> None:
    value = _contract()
    value["virtual_clock_name"] = "bad; exit"
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(TimingEvidenceError, match="clock name"):
        _load_contract(path)

    value = _contract()
    value["path_delay"] = "min"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(TimingEvidenceError, match="maximum-delay"):
        _load_contract(path)


def test_output_parser_requires_constrained_arrival_and_no_errors() -> None:
    text = """
HEPHAESTUS_BEGIN_UNCONSTRAINED
No paths found.
HEPHAESTUS_END_UNCONSTRAINED
HEPHAESTUS_BEGIN_MAX_PATHS
Startpoint: x_flat[0]
Endpoint: y_flat[0]
  1.250000 data arrival time
  998.750000 slack (MET)
HEPHAESTUS_END_MAX_PATHS
"""
    metrics = _parse_output(text, "dut")

    assert metrics["reported_path_count"] == 1
    assert metrics["worst_data_arrival_ns"] == 1.25
    assert metrics["minimum_reported_slack_ns"] == 998.75
    assert not metrics["unconstrained_paths_found"]

    with pytest.raises(TimingEvidenceError, match="reported errors"):
        _parse_output("Error: broken\n" + text, "dut")
    with pytest.raises(TimingEvidenceError, match="unconstrained"):
        _parse_output(text.replace("No paths found.", "Startpoint: x_flat[0]"), "dut")
    with pytest.raises(TimingEvidenceError, match="no constrained arrival"):
        _parse_output(text.replace("  1.250000 data arrival time\n", ""), "dut")


def test_safe_relative_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.v"
    inside.write_text("module inside; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _safe_relative(root, "inside.v") == inside.resolve()
    with pytest.raises(TimingEvidenceError, match="escapes"):
        _safe_relative(root, "../outside.v")
    with pytest.raises(TimingEvidenceError, match="relative"):
        _safe_relative(root, str(outside.resolve()))


def test_recursive_digest_binding() -> None:
    value = {"source": {"sha256": "abc"}, "items": [{"sha256": "def"}]}
    assert _contains_value(value, "abc")
    assert _contains_value(value, "def")
    assert not _contains_value(value, "missing")


def test_timing_comparisons_use_shared_dag() -> None:
    backends = {
        "shared_dag": {"metrics": {"worst_data_arrival_ns": 8.0}},
        "naive_shift_add": {"metrics": {"worst_data_arrival_ns": 10.0}},
    }
    comparison = _comparisons(backends)["naive_shift_add"]

    assert comparison["shared_dag_delay_difference_ns"] == 2.0
    assert comparison["shared_dag_delay_ratio"] == 0.8
    assert comparison["shared_dag_delay_reduction_percent"] == 20.0
