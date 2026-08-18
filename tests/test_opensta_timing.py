from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hephaestus.opensta_timing import (
    OpenSTATimingError,
    _analysis_script,
    _parse_single,
    _resolve_artifact,
    _signature,
    prepare_timing_evidence,
    run_timing_evidence,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepared_case(tmp_path: Path, program: str) -> tuple[Path, Path, Path]:
    root = tmp_path / "timing"
    run_dir = root / "analyses" / "shared_dag__unconstrained"
    run_dir.mkdir(parents=True)
    (run_dir / "analysis.tcl").write_text("exit 0\n", encoding="utf-8")
    _write_json(
        run_dir / "metadata.json",
        {
            "backend": "shared_dag",
            "label": "unconstrained",
            "module": "dut",
            "mapped_verilog_sha256": "1" * 64,
            "abc_library_area": 10.0,
            "abc_delay_picoseconds": 100.0,
            "virtual_clock_period_ns": 4.0,
            "input_delay_ns": 0.0,
            "output_delay_ns": 0.0,
            "driving_cell": "sg13g2_buf_4",
            "output_load_pf": 0.01,
            "analysis_script_sha256": _sha256(run_dir / "analysis.tcl"),
        },
    )
    _write_json(
        root / "prepared.json",
        {
            "schema": "hephaestus.opensta-sdc-prepared.v1",
            "source": {
                "abc_area_delay_evidence_sha256": "a" * 64,
                "liberty_sha256": "b" * 64,
            },
            "contract": {
                "combinational": True,
                "latency_cycles": 0,
            },
            "assumptions": {
                "virtual_clock_period_ns": 4.0,
                "input_delay_ns": 0.0,
                "output_delay_ns": 0.0,
                "driving_cell": "sg13g2_buf_4",
                "output_load_pf": 0.01,
                "parasitics": None,
                "wire_model": "test pre-layout model",
            },
            "analyses": [
                {
                    "backend": "shared_dag",
                    "label": "unconstrained",
                    "directory": "analyses/shared_dag__unconstrained",
                }
            ],
        },
    )
    executable = tmp_path / "fake_sta.py"
    executable.write_text(program, encoding="utf-8")
    executable.chmod(0o755)
    tool = tmp_path / "tool.json"
    _write_json(
        tool,
        {
            "schema": "hephaestus.opensta-tool.v1",
            "repository": "parallaxsw/OpenSTA",
            "commit": "c" * 40,
            "binary_sha256": _sha256(executable),
            "binary_reproducibility_verified": False,
        },
    )
    return root, executable, tool


def test_analysis_script_contains_the_explicit_contract() -> None:
    script = _analysis_script(
        module="dut",
        period_ns=4.0,
        input_delay_ns=0.0,
        output_delay_ns=0.0,
        driving_cell="sg13g2_buf_4",
        output_load_pf=0.01,
    )

    assert "link_design dut" in script
    assert "create_clock -name virtual_clock -period 4" in script
    assert "set_driving_cell -lib_cell sg13g2_buf_4" in script
    assert "set_load 0.01 [all_outputs]" in script
    assert "check_setup -verbose" in script
    assert "report_worst_slack -max -digits 9" in script


def test_signature_compares_report_contents_not_attempt_names() -> None:
    first = {
        "returncode": 0,
        "period_ns": 4.0,
        "worst_slack_ns": 2.0,
        "total_negative_slack_ns": 0.0,
        "derived_data_delay_ns": 2.0,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "stdout": "opensta.1.stdout.txt",
        "stderr": "opensta.1.stderr.txt",
    }
    second = {
        **first,
        "stdout": "opensta.2.stdout.txt",
        "stderr": "opensta.2.stderr.txt",
    }

    assert _signature(first) == _signature(second)


def test_parse_single_requires_one_finite_value() -> None:
    assert _parse_single(r"^value ([0-9.]+)$", "value 1.25", context="x") == 1.25
    with pytest.raises(OpenSTATimingError, match="expected one"):
        _parse_single(r"^value ([0-9.]+)$", "missing", context="x")
    with pytest.raises(OpenSTATimingError, match="expected one"):
        _parse_single(
            r"^value ([0-9.]+)$",
            "value 1\nvalue 2",
            context="x",
        )


def test_resolve_artifact_checks_path_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    artifact = root / "dut.v"
    artifact.write_text("module dut; endmodule\n", encoding="utf-8")
    entry = {"path": "dut.v", "sha256": _sha256(artifact)}

    assert _resolve_artifact(root, entry, context="dut") == artifact.resolve()
    with pytest.raises(OpenSTATimingError, match="escapes"):
        _resolve_artifact(
            root,
            {"path": "../dut.v", "sha256": _sha256(artifact)},
            context="dut",
        )
    with pytest.raises(OpenSTATimingError, match="digest mismatch"):
        _resolve_artifact(
            root,
            {"path": "dut.v", "sha256": "0" * 64},
            context="dut",
        )


def test_run_timing_evidence_accepts_repeatable_reports(tmp_path: Path) -> None:
    program = """#!/usr/bin/env python3
print("Startpoint: x")
print("Endpoint: y")
print("HEPHAESTUS_PERIOD_NS 4.0")
print("worst slack max 1.500000000")
print("tns max 0.000000000")
"""
    root, executable, tool = _prepared_case(tmp_path, program)

    result = run_timing_evidence(
        root,
        executable,
        tool,
        attempts=2,
        timeout_seconds=30,
    )

    timing = result["results"][0]["timing"]
    assert timing["derived_data_delay_ns"] == 2.5
    assert result["results"][0]["attempt_artifacts"] == [
        {
            "stdout": "opensta.1.stdout.txt",
            "stderr": "opensta.1.stderr.txt",
        },
        {
            "stdout": "opensta.2.stdout.txt",
            "stderr": "opensta.2.stderr.txt",
        },
    ]
    assert result["claims"]["repeatability_verified"]


def test_run_timing_evidence_rejects_report_drift(tmp_path: Path) -> None:
    program = """#!/usr/bin/env python3
from pathlib import Path
counter = Path("attempt.count")
value = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(value))
slack = 1.5 if value == 1 else 1.4
print("Startpoint: x")
print("Endpoint: y")
print("HEPHAESTUS_PERIOD_NS 4.0")
print(f"worst slack max {slack:.9f}")
print("tns max 0.000000000")
"""
    root, executable, tool = _prepared_case(tmp_path, program)

    with pytest.raises(OpenSTATimingError, match="not byte-identical"):
        run_timing_evidence(
            root,
            executable,
            tool,
            attempts=2,
            timeout_seconds=30,
        )


def test_prepare_rejects_invalid_boundary_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="period_ns"):
        prepare_timing_evidence(
            tmp_path,
            tmp_path / "out",
            period_ns=0.0,
            input_delay_ns=0.0,
            output_delay_ns=0.0,
            driving_cell="sg13g2_buf_4",
            output_load_pf=0.01,
            labels=("unconstrained",),
        )
    with pytest.raises(ValueError, match="driving_cell"):
        prepare_timing_evidence(
            tmp_path,
            tmp_path / "out",
            period_ns=4.0,
            input_delay_ns=0.0,
            output_delay_ns=0.0,
            driving_cell="bad; exit",
            output_load_pf=0.01,
            labels=("unconstrained",),
        )
    with pytest.raises(ValueError, match="unique"):
        prepare_timing_evidence(
            tmp_path,
            tmp_path / "out",
            period_ns=4.0,
            input_delay_ns=0.0,
            output_delay_ns=0.0,
            driving_cell="sg13g2_buf_4",
            output_load_pf=0.01,
            labels=("unconstrained", "unconstrained"),
        )
