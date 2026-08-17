from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.mapped import (
    MappedSynthesisError,
    _build_script,
    _comparisons,
    _inspect_liberty,
    _load_technology_config,
    _parse_library_area,
    _resolve_bundle_artifact,
    _verify_technology,
    build_mapped_synthesis_evidence,
)


def _technology_config(*, sha256: str, byte_count: int) -> dict[str, object]:
    return {
        "schema": "hephaestus.technology.v1",
        "technology_id": "test-technology",
        "provider": "Test",
        "process": "TEST1",
        "library": {
            "name": "test_lib",
            "corner": "typical",
            "nominal_voltage": 1.2,
            "nominal_temperature_c": 25,
            "area_unit": "liberty_library_area_unit",
            "sha256": sha256,
            "bytes": byte_count,
        },
        "source": {
            "repository": "example/test-pdk",
            "commit": "a" * 40,
            "path": "lib/test.lib",
            "url": "https://example.invalid/test.lib",
        },
        "flow": {
            "mapper": "yosys-abc",
            "timing_constraint": None,
            "physical_design": False,
        },
    }


def _write_liberty(path: Path) -> None:
    path.write_text(
        """
library (test_lib) {
  nom_voltage : 1.2;
  nom_temperature : 25;
  cell (test_inv) {
    area : 2.5;
    pin (A) { direction : input; }
    pin (Y) { direction : output; function : \"!A\"; }
  }
  cell (test_and) {
    area : 4.0;
    pin (A) { direction : input; }
    pin (B) { direction : input; }
    pin (Y) { direction : output; function : \"A & B\"; }
  }
}
""".lstrip(),
        encoding="utf-8",
    )


def test_mapping_script_loads_library_and_fails_closed() -> None:
    script = _build_script("safe_top")

    assert script.startswith("read_liberty -lib ../../technology/technology.lib")
    assert "hierarchy -check -top safe_top" in script
    assert "abc -liberty ../../technology/technology.lib" in script
    assert "check -assert" in script
    assert "stat -liberty ../../technology/technology.lib" in script


def test_mapping_script_rejects_unsafe_names_and_paths() -> None:
    with pytest.raises(MappedSynthesisError, match="unsafe"):
        _build_script("top; delete *")
    with pytest.raises(MappedSynthesisError, match="unsafe Liberty path"):
        _build_script("top", "library.lib; shell touch escaped")


def test_liberty_inspection_extracts_cell_areas(tmp_path: Path) -> None:
    liberty = tmp_path / "test.lib"
    _write_liberty(liberty)

    metadata, areas = _inspect_liberty(liberty)

    assert metadata["library"] == "test_lib"
    assert metadata["nominal_voltage"] == 1.2
    assert metadata["nominal_temperature_c"] == 25.0
    assert metadata["cell_declarations"] == 2
    assert metadata["cells_with_area"] == 2
    assert metadata["minimum_cell_area"] == 2.5
    assert metadata["maximum_cell_area"] == 4.0
    assert areas == {"test_inv": 2.5, "test_and": 4.0}


def test_technology_config_and_liberty_must_match(tmp_path: Path) -> None:
    liberty = tmp_path / "test.lib"
    _write_liberty(liberty)
    metadata, _ = _inspect_liberty(liberty)
    config_path = tmp_path / "technology.json"
    config = _technology_config(
        sha256=str(metadata["sha256"]),
        byte_count=int(metadata["bytes"]),
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = _load_technology_config(config_path)
    _verify_technology(loaded, metadata)

    loaded["library"]["sha256"] = "0" * 64  # type: ignore[index]
    with pytest.raises(MappedSynthesisError, match="sha256"):
        _verify_technology(loaded, metadata)


def test_technology_config_rejects_unpinned_or_physical_flows(tmp_path: Path) -> None:
    config = _technology_config(sha256="0" * 64, byte_count=1)
    config["source"]["commit"] = "main"  # type: ignore[index]
    path = tmp_path / "technology.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(MappedSynthesisError, match="full lowercase Git commit"):
        _load_technology_config(path)

    config = _technology_config(sha256="0" * 64, byte_count=1)
    config["flow"]["physical_design"] = True  # type: ignore[index]
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(MappedSynthesisError, match="physical_design false"):
        _load_technology_config(path)


def test_parse_library_area_requires_one_positive_result() -> None:
    report = """
=== top ===
   Number of cells: 12
   Chip area for module '\\top': 123.500000
"""
    assert _parse_library_area(report, "top") == 123.5

    with pytest.raises(MappedSynthesisError, match="expected one"):
        _parse_library_area("Number of cells: 12\n", "top")
    with pytest.raises(MappedSynthesisError, match="invalid"):
        _parse_library_area("Chip area for module '\\top': 0.0\n", "top")


def test_bundle_artifact_cannot_escape_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inside = bundle / "input.sv"
    inside.write_text("module input_core; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.sv"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _resolve_bundle_artifact(bundle, "input.sv") == inside.resolve()
    with pytest.raises(MappedSynthesisError, match="escapes"):
        _resolve_bundle_artifact(bundle, "../outside.sv")
    with pytest.raises(MappedSynthesisError, match="relative"):
        _resolve_bundle_artifact(bundle, str(outside.resolve()))


def test_comparisons_use_shared_dag_as_the_reference() -> None:
    backends = {
        "shared_dag": {"library_area": 80.0, "metrics": {"cell_count": 8}},
        "naive_shift_add": {"library_area": 100.0, "metrics": {"cell_count": 10}},
    }

    comparisons = _comparisons(backends)

    assert comparisons["naive_shift_add"]["shared_dag_area_difference"] == 20.0
    assert comparisons["naive_shift_add"]["shared_dag_area_ratio"] == 0.8
    assert comparisons["naive_shift_add"]["shared_dag_area_reduction_percent"] == 20.0
    assert comparisons["naive_shift_add"]["shared_dag_cell_difference"] == 2
    assert comparisons["naive_shift_add"]["shared_dag_cell_reduction_percent"] == 20.0


def test_mapping_requires_a_verified_matched_contract(tmp_path: Path) -> None:
    bundle = tmp_path / "matched"
    bundle.mkdir()
    (bundle / "matched_manifest.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.matched-baselines.v1",
                "backends": {},
                "claims": {"matched_integer_contract_verified": False},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MappedSynthesisError, match="must be verified"):
        build_mapped_synthesis_evidence(
            bundle,
            tmp_path / "missing-technology.json",
            tmp_path / "missing.lib",
            tmp_path / "out",
            yosys="missing-yosys",
        )
