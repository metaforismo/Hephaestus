from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hephaestus.abc_timing_formal import (
    AbcAreaDelayFormalError,
    _repeatability_verified,
    _require_source_claims,
    _safe_label,
    _select_proof_representatives,
    build_abc_area_delay_formal_evidence,
)
from hephaestus.report import sha256_file


def _source_claims() -> dict[str, bool]:
    return {
        "matched_integer_contract_verified": True,
        "technology_aware_abc_mapping_performed": True,
        "declared_input_driver_model_used": True,
        "declared_output_load_used": True,
        "abc_internal_timing_estimated": True,
        "abc_delay_targets_swept": True,
        "target_attainment_evaluated": True,
        "mapped_netlist_structurally_checked": True,
        "post_mapping_library_area_estimated": True,
        "area_delay_product_computed": True,
        "mapped_gate_level_equivalence_verified": False,
    }


def _repeatable_run() -> dict[str, object]:
    return {
        "repeatability": {
            "performed": True,
            "passed": True,
            "byte_identical_artifacts": {
                "mapped_netlist": True,
                "mapped_verilog": True,
                "mapped_stat": True,
            },
            "normalized_metrics_identical": True,
        }
    }


def test_safe_label_rejects_paths_and_shell_syntax() -> None:
    assert _safe_label("d4000ps", context="run") == "d4000ps"

    for value in ("../escape", "a/b", "x;touch", "space label"):
        with pytest.raises(AbcAreaDelayFormalError, match="unsafe label"):
            _safe_label(value, context="run")


def test_source_claims_must_be_complete_and_layered() -> None:
    claims = _source_claims()
    assert _require_source_claims(claims) is claims

    claims["abc_internal_timing_estimated"] = False
    with pytest.raises(AbcAreaDelayFormalError, match="required true claims"):
        _require_source_claims(claims)

    claims = _source_claims()
    claims["mapped_gate_level_equivalence_verified"] = True
    with pytest.raises(AbcAreaDelayFormalError, match="must not pre-claim"):
        _require_source_claims(claims)


def test_repeatability_must_be_complete() -> None:
    run = _repeatable_run()
    _repeatability_verified(run, context="run")

    artifacts = run["repeatability"]["byte_identical_artifacts"]  # type: ignore[index]
    artifacts["mapped_verilog"] = False
    with pytest.raises(AbcAreaDelayFormalError, match="non-identical"):
        _repeatability_verified(run, context="run")


def test_representatives_prefer_pareto_labels_and_collapse_identical_rtl() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    runs = {
        "d1000ps": {"mapped_verilog_sha256": digest_a},
        "d2000ps": {"mapped_verilog_sha256": digest_a},
        "unconstrained": {"mapped_verilog_sha256": digest_a},
        "d4000ps": {"mapped_verilog_sha256": digest_b},
        "d8000ps": {"mapped_verilog_sha256": digest_b},
        "d16000ps": {"mapped_verilog_sha256": digest_b},
    }

    representatives, aliases = _select_proof_representatives(
        runs,
        ["unconstrained", "d4000ps"],
    )

    assert aliases == {
        "unconstrained": ["d1000ps", "d2000ps", "unconstrained"],
        "d4000ps": ["d16000ps", "d4000ps", "d8000ps"],
    }
    assert representatives["d1000ps"] == "unconstrained"
    assert representatives["d16000ps"] == "d4000ps"
    assert representatives["unconstrained"] == "unconstrained"


def test_representatives_reject_missing_pareto_labels_and_bad_digests() -> None:
    with pytest.raises(AbcAreaDelayFormalError, match="absent"):
        _select_proof_representatives(
            {"unconstrained": {"mapped_verilog_sha256": "a" * 64}},
            ["d4000ps"],
        )

    with pytest.raises(AbcAreaDelayFormalError, match="valid mapped-Verilog"):
        _select_proof_representatives(
            {"unconstrained": {"mapped_verilog_sha256": "not-a-digest"}},
            ["unconstrained"],
        )


def test_builder_rejects_unverified_source_before_tool_resolution(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "evidence"
    bundle.mkdir()
    (bundle / "abc_area_delay_evidence.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.abc-area-delay-evidence.v1",
                "evidence_level": "abc_liberty_area_delay_estimate",
                "claims": {
                    **_source_claims(),
                    "mapped_netlist_structurally_checked": False,
                },
            }
        ),
        encoding="utf-8",
    )
    codes = tmp_path / "codes.npy"
    np.save(codes, np.asarray([[1]], dtype=np.int64), allow_pickle=False)

    with pytest.raises(AbcAreaDelayFormalError, match="required true claims"):
        build_abc_area_delay_formal_evidence(
            bundle,
            codes,
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_builder_enforces_codes_digest_and_formal_width_before_yosys(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "evidence"
    bundle.mkdir()
    codes = tmp_path / "codes.npy"
    np.save(codes, np.asarray([[1, 1]], dtype=np.int64), allow_pickle=False)
    matched = bundle / "source_matched_manifest.json"
    matched.write_text(
        json.dumps(
            {
                "schema": "hephaestus.matched-baselines.v1",
                "contract": {
                    "domain": "quantized_integer_core_before_row_scaling",
                    "input_count": 2,
                    "output_count": 1,
                    "input_width": 8,
                    "accumulator_width": 10,
                    "combinational": True,
                    "latency_cycles": 0,
                },
                "claims": {"matched_integer_contract_verified": True},
                "artifact_sha256": {"source_codes": sha256_file(codes)},
                "backends": {"shared_dag": {"module": "shared_dag"}},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "abc_area_delay_evidence.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.abc-area-delay-evidence.v1",
                "evidence_level": "abc_liberty_area_delay_estimate",
                "claims": _source_claims(),
                "source": {
                    "matched_manifest": matched.name,
                    "matched_manifest_sha256": sha256_file(matched),
                },
                "contract": {
                    "domain": "quantized_integer_core_before_row_scaling",
                    "input_count": 2,
                    "output_count": 1,
                    "input_width": 8,
                    "accumulator_width": 10,
                    "combinational": True,
                    "latency_cycles": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AbcAreaDelayFormalError, match="exceeds"):
        build_abc_area_delay_formal_evidence(
            bundle,
            codes,
            tmp_path / "out",
            yosys="missing-yosys",
            max_input_bits=8,
        )
