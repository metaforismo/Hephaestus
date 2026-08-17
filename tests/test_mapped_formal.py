from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hephaestus.mapped_formal import (
    MappedFormalError,
    _inspect_liberty_functions,
    _proof_script,
    _resolve_bundle_artifact,
    build_mapped_formal_evidence,
)
from hephaestus.report import sha256_file


def _write_liberty(path: Path, *, functional: bool = True) -> None:
    function = 'function : "!A";' if functional else ""
    path.write_text(
        f"""
library (test_lib) {{
  cell (test_inv) {{
    area : 2.5;
    pin (A) {{ direction : input; }}
    pin (Y) {{ direction : output; {function} }}
  }}
}}
""".lstrip(),
        encoding="utf-8",
    )


def _write_fake_yosys(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

if '-V' in sys.argv:
    print('Yosys test 1.0')
elif Path.cwd().name == 'negative_control':
    print('SAT proof finished - model found: FAIL!')
else:
    print('SAT proof finished - no model found: SUCCESS!')
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_mapped_bundle(
    root: Path,
    codes_path: Path,
    *,
    mapping_verified: bool = True,
    functional_liberty: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    contract = {
        "domain": "quantized_integer_core_before_row_scaling",
        "input_count": 2,
        "output_count": 1,
        "input_width": 4,
        "accumulator_width": 8,
        "combinational": True,
        "latency_cycles": 0,
    }
    matched_manifest = {
        "schema": "hephaestus.matched-baselines.v1",
        "contract": contract,
        "backends": {
            "shared_dag": {
                "module": "mapped_shared",
                "rtl": "shared_dag.sv",
            }
        },
        "artifact_sha256": {"source_codes": sha256_file(codes_path)},
        "claims": {"matched_integer_contract_verified": True},
    }
    matched_path = root / "source_matched_manifest.json"
    matched_path.write_text(
        json.dumps(matched_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    technology_dir = root / "technology"
    technology_dir.mkdir()
    liberty_path = technology_dir / "technology.lib"
    _write_liberty(liberty_path, functional=functional_liberty)
    config_path = technology_dir / "technology.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": "hephaestus.technology.v1",
                "technology_id": "test-technology",
                "library": {
                    "name": "test_lib",
                    "sha256": sha256_file(liberty_path),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    backend_dir = root / "backends" / "shared_dag"
    backend_dir.mkdir(parents=True)
    mapped_verilog = backend_dir / "mapped.v"
    mapped_verilog.write_text(
        "module mapped_shared(input [7:0] x_flat, output [7:0] y_flat); "
        "assign y_flat = x_flat; endmodule\n",
        encoding="utf-8",
    )

    mapped_manifest = {
        "schema": "hephaestus.standard-cell-mapped-evidence.v1",
        "evidence_level": "standard_cell_mapped_area_estimate",
        "source": {
            "matched_manifest": matched_path.name,
            "matched_manifest_sha256": sha256_file(matched_path),
        },
        "technology": {
            "technology_id": "test-technology",
            "library": {
                "name": "test_lib",
                "sha256": sha256_file(liberty_path),
            },
            "configuration_artifact": {
                "path": "technology/technology.json",
                "sha256": sha256_file(config_path),
            },
            "liberty_artifact": {
                "path": "technology/technology.lib",
                "sha256": sha256_file(liberty_path),
            },
        },
        "contract": contract,
        "backends": {
            "shared_dag": {
                "module": "mapped_shared",
                "metrics": {
                    "input_bits": 8,
                    "output_bits": 8,
                    "cell_count": 1,
                    "cell_type_histogram": {"test_inv": 1},
                },
                "artifacts": {
                    "mapped_verilog": {
                        "path": "backends/shared_dag/mapped.v",
                        "sha256": sha256_file(mapped_verilog),
                    }
                },
            }
        },
        "claims": {
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": mapping_verified,
            "mapped_netlist_structurally_checked": mapping_verified,
        },
    }
    (root / "mapped_evidence.json").write_text(
        json.dumps(mapped_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def test_proof_script_loads_functional_liberty_models() -> None:
    positive = _proof_script(
        miter_module="positive_miter",
        expect_counterexample=False,
    )
    negative = _proof_script(
        miter_module="negative_miter",
        expect_counterexample=True,
    )

    assert positive.startswith("read_liberty -ignore_miss_func ../../technology/technology.lib")
    assert "hierarchy -check -top positive_miter" in positive
    assert "check -assert" in positive
    assert "sat -verify -set-def-inputs" in positive
    assert "sat -set-def-inputs" in negative
    assert "-verify" not in negative


def test_liberty_inspection_identifies_functional_cells(tmp_path: Path) -> None:
    liberty = tmp_path / "test.lib"
    _write_liberty(liberty)

    metadata, functional_cells = _inspect_liberty_functions(liberty)

    assert metadata["library"] == "test_lib"
    assert metadata["cell_declarations"] == 1
    assert metadata["functional_cell_models"] == 1
    assert functional_cells == {"test_inv": ("!A",)}


def test_bundle_artifact_cannot_escape_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inside = bundle / "mapped.v"
    inside.write_text("module mapped; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _resolve_bundle_artifact(bundle, "mapped.v") == inside.resolve()
    with pytest.raises(MappedFormalError, match="escapes"):
        _resolve_bundle_artifact(bundle, "../outside.v")


def test_mapped_formal_requires_verified_mapping(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, -1]], dtype=np.int64), allow_pickle=False)
    bundle = _write_mapped_bundle(
        tmp_path / "mapped",
        codes_path,
        mapping_verified=False,
    )

    with pytest.raises(MappedFormalError, match="mapping must be completed"):
        build_mapped_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_mapped_formal_requires_function_models_for_every_used_cell(
    tmp_path: Path,
) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, -1]], dtype=np.int64), allow_pickle=False)
    bundle = _write_mapped_bundle(
        tmp_path / "mapped",
        codes_path,
        functional_liberty=False,
    )

    with pytest.raises(MappedFormalError, match="without functional Liberty models"):
        build_mapped_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_mapped_formal_enforces_the_input_width_limit(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, -1]], dtype=np.int64), allow_pickle=False)
    bundle = _write_mapped_bundle(tmp_path / "mapped", codes_path)

    with pytest.raises(MappedFormalError, match="exceeds the configured limit"):
        build_mapped_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
            max_input_bits=4,
        )


def test_mapped_formal_rejects_a_codes_digest_mismatch(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, -1]], dtype=np.int64), allow_pickle=False)
    bundle = _write_mapped_bundle(tmp_path / "mapped", codes_path)
    np.save(codes_path, np.asarray([[1, 1]], dtype=np.int64), allow_pickle=False)

    with pytest.raises(MappedFormalError, match="source codes do not match"):
        build_mapped_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_mapped_formal_builds_self_contained_positive_and_negative_evidence(
    tmp_path: Path,
) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, -1]], dtype=np.int64), allow_pickle=False)
    bundle = _write_mapped_bundle(tmp_path / "mapped", codes_path)
    fake_yosys = tmp_path / "fake-yosys"
    _write_fake_yosys(fake_yosys)

    manifest = build_mapped_formal_evidence(
        bundle,
        codes_path,
        tmp_path / "out",
        yosys=str(fake_yosys),
        max_input_bits=16,
        timeout_seconds=10,
    )

    assert manifest["schema"] == "hephaestus.mapped-formal-equivalence-evidence.v1"
    assert manifest["evidence_level"] == "yosys_sat_standard_cell_mapped_equivalence"
    assert manifest["backends"]["shared_dag"]["proof"]["passed"]
    assert manifest["backends"]["shared_dag"]["functional_library_models_verified"]
    assert manifest["negative_control"]["proof"]["counterexample_found"]
    assert manifest["claims"]["mapped_gate_level_equivalence_verified"]
    assert manifest["claims"]["liberty_functional_models_verified"]
    assert manifest["claims"]["post_mapping_library_area_estimated"]
    assert not manifest["claims"]["post_synthesis_ppa_measured"]
    assert not manifest["claims"]["timing_analyzed"]
    assert (tmp_path / "out" / "mapped_formal_evidence.json").is_file()
    source_mapped = json.loads(
        (tmp_path / "out" / "source_mapped_evidence.json").read_text(encoding="utf-8")
    )
    assert source_mapped["schema"] == "hephaestus.standard-cell-mapped-evidence.v1"
    assert (tmp_path / "out" / "SUMMARY.md").is_file()
    assert (tmp_path / "out" / "technology" / "technology.lib").is_file()
