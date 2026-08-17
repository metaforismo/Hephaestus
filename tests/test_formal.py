from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hephaestus.formal import (
    FormalError,
    _proof_script,
    _resolve_bundle_artifact,
    build_formal_evidence,
    emit_miter_systemverilog,
    emit_reference_systemverilog,
)
from hephaestus.report import sha256_file


def _write_matched_manifest(
    root: Path,
    codes_path: Path,
    *,
    verified: bool = True,
    input_count: int = 2,
    output_count: int = 1,
    input_width: int = 4,
    accumulator_width: int = 7,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "matched_manifest.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.matched-baselines.v1",
                "contract": {
                    "domain": "quantized_integer_core_before_row_scaling",
                    "input_count": input_count,
                    "output_count": output_count,
                    "input_width": input_width,
                    "accumulator_width": accumulator_width,
                },
                "backends": {
                    "shared_dag": {
                        "module": "shared_dag",
                        "rtl": "shared_dag.sv",
                    }
                },
                "artifact_sha256": {
                    "source_codes": sha256_file(codes_path),
                    "shared_dag_rtl": "unused-before-yosys-resolution",
                },
                "claims": {"matched_integer_contract_verified": verified},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_reference_is_derived_directly_from_codes() -> None:
    codes = np.asarray([[1, -2, 0], [4, 1, -1]], dtype=np.int64)
    rtl = emit_reference_systemverilog(
        codes,
        input_width=8,
        accumulator_width=12,
        module_name="formal_reference",
    )

    assert "module formal_reference" in rtl
    assert "always @* begin" in rtl
    assert "$signed(sx_0) * 12'sd1" in rtl
    assert "$signed(sx_1) * -12'sd2" in rtl
    assert "product_o0_i2" not in rtl
    assert "CompilationPlan" not in rtl


def test_reference_rejects_an_unsafe_accumulator() -> None:
    codes = np.asarray([[4, 4]], dtype=np.int64)
    with pytest.raises(ValueError, match="unsafe"):
        emit_reference_systemverilog(
            codes,
            input_width=8,
            accumulator_width=8,
            module_name="too_narrow",
        )


def test_miter_negative_control_is_data_dependent() -> None:
    normal = emit_miter_systemverilog(
        dut_module="dut",
        reference_module="reference_core",
        input_bits=16,
        output_bits=8,
        module_name="normal_miter",
    )
    faulted = emit_miter_systemverilog(
        dut_module="dut",
        reference_module="reference_core",
        input_bits=16,
        output_bits=8,
        module_name="faulted_miter",
        inject_fault=True,
    )

    assert "assign mismatch = |(y_dut ^ y_reference);" in normal
    assert "x_flat[0]" not in normal.split("assign mismatch", maxsplit=1)[0]
    assert "assign y_faulted = y_dut ^ fault_mask;" in faulted
    assert "x_flat[0]" in faulted


def test_proof_script_enables_verify_only_for_positive_proofs() -> None:
    positive = _proof_script(
        miter_module="positive_miter",
        expect_counterexample=False,
    )
    negative = _proof_script(
        miter_module="negative_miter",
        expect_counterexample=True,
    )

    assert "sat -verify -set-def-inputs" in positive
    assert "sat -set-def-inputs" in negative
    assert "-verify" not in negative
    assert "-prove mismatch 0" in positive


def test_bundle_artifact_cannot_escape_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inside = bundle / "dut.sv"
    inside.write_text("module dut; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.sv"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _resolve_bundle_artifact(bundle, "dut.sv") == inside.resolve()
    with pytest.raises(FormalError, match="escapes"):
        _resolve_bundle_artifact(bundle, "../outside.sv")


def test_formal_evidence_requires_a_verified_matched_contract(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, 0]], dtype=np.int64), allow_pickle=False)
    bundle = tmp_path / "matched"
    _write_matched_manifest(bundle, codes_path, verified=False)

    with pytest.raises(FormalError, match="must be verified"):
        build_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_formal_evidence_enforces_the_input_width_limit(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, 0]], dtype=np.int64), allow_pickle=False)
    bundle = tmp_path / "matched"
    _write_matched_manifest(
        bundle,
        codes_path,
        input_count=2,
        input_width=8,
        accumulator_width=9,
    )

    with pytest.raises(FormalError, match="exceeds the configured limit"):
        build_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
            max_input_bits=8,
        )


def test_formal_evidence_rejects_a_shape_mismatch(tmp_path: Path) -> None:
    codes_path = tmp_path / "codes.npy"
    np.save(codes_path, np.asarray([[1, 0, 1]], dtype=np.int64), allow_pickle=False)
    bundle = tmp_path / "matched"
    _write_matched_manifest(bundle, codes_path)

    with pytest.raises(FormalError, match="does not match the contract"):
        build_formal_evidence(
            bundle,
            codes_path,
            tmp_path / "out",
            yosys="missing-yosys",
        )
