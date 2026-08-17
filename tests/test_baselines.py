from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hephaestus.baselines import (
    _pack_lanes,
    build_matched_baselines,
    emit_constant_multiplier_systemverilog,
    emit_equivalence_testbench,
    emit_naive_shift_add_systemverilog,
)
from hephaestus.emit_sv import emit_systemverilog
from hephaestus.lower import lower_codes


def _write_compiled_artifact(root: Path) -> tuple[np.ndarray, str]:
    codes = np.asarray(
        [
            [1, -2, 0, 4],
            [1, -2, 4, 0],
            [0, 1, -1, 2],
        ],
        dtype=np.int64,
    )
    plan = lower_codes(codes, input_width=8, enable_cse=True)
    module = "hephaestus_test_shared"
    rtl = emit_systemverilog(plan, module_name=module)

    root.mkdir(parents=True)
    np.save(root / "codes.npy", codes, allow_pickle=False)
    (root / "plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / f"{module}.sv").write_text(rtl, encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.manifest.v1",
                "topology": {
                    "module": module,
                    "input_width": plan.input_width,
                    "accumulator_width": plan.accumulator_width,
                },
                "artifacts": {"systemverilog": f"{module}.sv"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return codes, module


def test_pack_lanes_uses_flattened_little_lane_order() -> None:
    assert _pack_lanes([1, -1, -128], 8) == 0x80FF01


def test_emitters_share_the_flattened_contract() -> None:
    codes = np.asarray([[1, -2, 4], [0, 1, -1]], dtype=np.int64)
    plan = lower_codes(codes, input_width=8)

    multiplier = emit_constant_multiplier_systemverilog(
        codes,
        input_width=plan.input_width,
        accumulator_width=plan.accumulator_width,
        module_name="multiplier_baseline",
    )
    naive = emit_naive_shift_add_systemverilog(
        codes,
        input_width=plan.input_width,
        accumulator_width=plan.accumulator_width,
        module_name="naive_baseline",
    )

    assert "input  wire signed [23:0] x_flat" in multiplier
    assert "input  wire signed [23:0] x_flat" in naive
    assert "$signed(sx_0) *" in multiplier
    assert "<<< 2" in naive
    assert "cross-output" not in naive.lower()


def test_naive_backend_rejects_non_power_of_two_codes() -> None:
    codes = np.asarray([[3, 1]], dtype=np.int64)
    with pytest.raises(ValueError, match="signed power of two"):
        emit_naive_shift_add_systemverilog(
            codes,
            input_width=8,
            accumulator_width=12,
            module_name="invalid",
        )


def test_equivalence_testbench_is_deterministic() -> None:
    first, first_count = emit_equivalence_testbench(
        shared_module="shared",
        multiplier_module="multiplier",
        naive_module="naive",
        input_count=3,
        output_count=2,
        input_width=8,
        accumulator_width=12,
        random_vectors=16,
        seed=7,
        module_name="matched_tb",
    )
    second, second_count = emit_equivalence_testbench(
        shared_module="shared",
        multiplier_module="multiplier",
        naive_module="naive",
        input_count=3,
        output_count=2,
        input_width=8,
        accumulator_width=12,
        random_vectors=16,
        seed=7,
        module_name="matched_tb",
    )

    assert first == second
    assert first_count == second_count
    assert first_count >= 16
    assert "$fatal(1)" in first


def test_build_matched_baselines_writes_self_contained_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "matched"
    codes, shared_module = _write_compiled_artifact(source)

    manifest = build_matched_baselines(
        source,
        output,
        random_vectors=8,
        seed=11,
        simulate=False,
    )

    assert manifest["schema"] == "hephaestus.matched-baselines.v1"
    assert manifest["contract"]["input_count"] == codes.shape[1]
    assert manifest["contract"]["output_count"] == codes.shape[0]
    assert manifest["backends"]["shared_dag"]["module"] == shared_module
    assert manifest["backends"]["naive_shift_add"]["cross_output_sharing"] is False
    assert manifest["backends"]["constant_multipliers"]["source_multiply_operators"] == int(
        np.count_nonzero(codes)
    )
    assert manifest["claims"] == {
        "matched_integer_contract_verified": False,
        "post_synthesis_ppa_measured": False,
        "post_layout_pex_verified": False,
        "silicon_verified": False,
    }

    for filename in (
        "shared_dag.sv",
        "constant_multipliers.sv",
        "naive_shift_add.sv",
        "matched_testbench.sv",
        "matched_manifest.json",
    ):
        assert (output / filename).is_file()
