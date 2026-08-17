from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.abc_timing import (
    AbcTimingError,
    _build_script,
    _constraints_text,
    _inspect_liberty,
    _load_area_delay_config,
    _pareto_labels,
    _parse_stime,
    _run_mapping,
    _target_label,
    _verify_inputs,
)


def _write_liberty(path: Path, *, driver_dont_use: bool = False) -> None:
    dont_use = "dont_use : true;" if driver_dont_use else ""
    path.write_text(
        f"""
        library (test_lib) {{
          nom_voltage : 1.2;
          nom_temperature : 25;
          cell (sg13g2_inv_1) {{
            area : 1.0;
            pin (A) {{ direction : input; }}
            pin (Y) {{ direction : output; function : \"!A\"; }}
          }}
          cell (sg13g2_buf_4) {{
            area : 2.0;
            {dont_use}
            pin (A) {{ direction : input; }}
            pin (X) {{ direction : output; function : \"A\"; }}
          }}
        }}
        """,
        encoding="utf-8",
    )


def _write_config(path: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "hephaestus.abc-area-delay-config.v1",
                "evidence_id": "test",
                "technology": {
                    "technology_id": "test-tech",
                    "liberty_name": "test_lib",
                    "liberty_sha256": digest,
                },
                "io": {
                    "driving_cell": "sg13g2_buf_4",
                    "output_load_femtofarads": 10.0,
                },
                "targets_picoseconds": [None, 1000, 2000],
                "flow": {
                    "mapper": "yosys-abc",
                    "timing_model": "abc-stime",
                    "physical_design": False,
                    "signoff_sta": False,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_yosys(path: Path, *, include_delay: bool = True) -> None:
    delay_line = (
        "print('ABC: WireLoad = \"none\"  Gates = 1  Cap = 1.0 ff  Area = 1.00  Delay = 2.50 ps')"
        if include_delay
        else ""
    )
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

if '-V' in sys.argv:
    print('Yosys 0.33 test')
    raise SystemExit(0)
script = Path('map.ys').read_text(encoding='utf-8')
top = re.search(r'hierarchy -check -top ([A-Za-z0-9_$]+)', script).group(1)
netlist = {{
    'modules': {{
        top: {{
            'ports': {{
                'inputs': {{'direction': 'input', 'bits': [2]}},
                'outputs': {{'direction': 'output', 'bits': [3]}},
            }},
            'cells': {{
                'cell0': {{
                    'type': 'sg13g2_inv_1',
                    'connections': {{'A': [2], 'Y': [3]}},
                }}
            }},
            'netnames': {{}},
            'memories': {{}},
        }}
    }}
}}
Path('mapped.json').write_text(json.dumps(netlist), encoding='utf-8')
Path('mapped.v').write_text('module ' + top + '; endmodule\\n', encoding='utf-8')
Path('mapped.stat.txt').write_text(
    "Chip area for module '\\\\" + top + "': 1.000000\\n",
    encoding='utf-8',
)
print('ABC: Library "test_lib" from "test.lib" has 2 cells (0 skipped: 0 seq;)')
{delay_line}
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_parse_stime_extracts_declared_units() -> None:
    output = (
        'ABC: WireLoad = "none"  Gates = 497 (2.6 %)  Cap = 8.5 ff '
        "Area = 5439.57 (94.2 %)  Delay = 2029.49 ps (17.3 %)"
    )
    parsed = _parse_stime(output)
    assert parsed["gate_count"] == 497
    assert parsed["library_area"] == 5439.57
    assert parsed["delay"] == 2029.49
    assert parsed["delay_unit"] == "ps"
    assert parsed["capacitance_unit"] == "ff"


def test_parse_stime_rejects_missing_delay() -> None:
    with pytest.raises(AbcTimingError, match="exactly one ABC stime"):
        _parse_stime('ABC: WireLoad = "none" Gates = 1 Area = 1.0')


def test_target_labels_and_constraints_are_deterministic() -> None:
    assert _target_label(None) == "unconstrained"
    assert _target_label(4000) == "d4000ps"
    assert _constraints_text("sg13g2_buf_4", 10.0) == (
        "set_driving_cell sg13g2_buf_4\nset_load 10\n"
    )


def test_build_script_declares_constraints_and_target() -> None:
    script = _build_script("top", 4000)
    assert "abc -liberty" in script
    assert "-constr ../../../constraints/abc.constr" in script
    assert "-D 4000" in script
    assert "check -assert" in script
    assert "stat -liberty" in script


def test_config_requires_one_leading_unconstrained_target(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(path, "0" * 64)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["targets_picoseconds"] = [1000, None, 2000]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AbcTimingError, match="start with exactly one null"):
        _load_area_delay_config(path)


def test_liberty_driver_must_be_usable(tmp_path: Path) -> None:
    liberty = tmp_path / "test.lib"
    _write_liberty(liberty, driver_dont_use=True)
    metadata, _, flags = _inspect_liberty(liberty)
    config = {
        "technology": {
            "technology_id": "test-tech",
            "liberty_name": "test_lib",
            "liberty_sha256": metadata["sha256"],
        },
        "io": {"driving_cell": "sg13g2_buf_4"},
    }
    technology = {
        "technology_id": "test-tech",
        "library": {
            "name": "test_lib",
            "sha256": metadata["sha256"],
            "bytes": metadata["bytes"],
        },
    }
    with pytest.raises(AbcTimingError, match="marked dont_use"):
        _verify_inputs(config, technology, metadata, flags)


def test_pareto_front_collapses_duplicate_target_points() -> None:
    runs = {
        "unconstrained": {
            "library_area": 10.0,
            "critical_path_delay_picoseconds": 2.0,
        },
        "d1000ps": {
            "library_area": 10.0,
            "critical_path_delay_picoseconds": 2.0,
        },
        "d4000ps": {
            "library_area": 9.0,
            "critical_path_delay_picoseconds": 2.2,
        },
        "dominated": {
            "library_area": 11.0,
            "critical_path_delay_picoseconds": 2.3,
        },
    }
    assert _pareto_labels(runs) == ["unconstrained", "d4000ps"]


def test_run_mapping_cross_checks_stime_stat_and_histogram(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yosys"
    _fake_yosys(executable)
    source = tmp_path / "source.sv"
    source.write_text("module top(input inputs, output outputs); endmodule\n")
    run_dir = tmp_path / "out" / "runs" / "shared_dag" / "unconstrained"
    result = _run_mapping(
        source_rtl=source,
        module="top",
        target_picoseconds=None,
        run_dir=run_dir,
        executable=str(executable),
        cell_areas={"sg13g2_inv_1": 1.0},
        expected_input_bits=1,
        expected_output_bits=1,
        timeout_seconds=30,
    )
    assert result["metrics"]["cell_count"] == 1
    assert result["library_area"] == 1.0
    assert result["critical_path_delay_picoseconds"] == 2.5
    assert result["area_cross_check_passed"]


def test_run_mapping_fails_closed_when_stime_is_absent(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yosys"
    _fake_yosys(executable, include_delay=False)
    source = tmp_path / "source.sv"
    source.write_text("module top(input inputs, output outputs); endmodule\n")
    run_dir = tmp_path / "out" / "runs" / "shared_dag" / "unconstrained"
    with pytest.raises(AbcTimingError, match="exactly one ABC stime"):
        _run_mapping(
            source_rtl=source,
            module="top",
            target_picoseconds=None,
            run_dir=run_dir,
            executable=str(executable),
            cell_areas={"sg13g2_inv_1": 1.0},
            expected_input_bits=1,
            expected_output_bits=1,
            timeout_seconds=30,
        )
