from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "flows"
    / "openroad"
    / "post_physical_equivalence"
    / "run_probe.py"
)
SPEC = importlib.util.spec_from_file_location("post_physical_probe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_registered_contract_preserves_one_cycle_boundary() -> None:
    text = probe.emit_registered_contract(
        reference_core_module="reference_core",
        module_name="registered_contract",
        input_bits=48,
        output_bits=48,
    )

    assert "always @(posedge clk)" in text
    assert "if (reset)" in text
    assert "x_q <= x_flat;" in text
    assert "valid_q <= valid_in;" in text
    assert "valid_out <= valid_q;" in text
    assert "y_flat <= y_comb;" in text


@pytest.mark.parametrize("fault", ["none", "data", "valid", "reset"])
def test_miter_emits_explicit_fault_modes(fault: str) -> None:
    text = probe.emit_miter(
        dut_module="routed",
        reference_module="reference",
        module_name=f"miter_{fault}",
        input_bits=48,
        output_bits=48,
        fault=fault,
    )

    assert "assign mismatch =" in text
    if fault == "data":
        assert "data_fault_mask" in text
        assert "faulted_y" in text
    elif fault == "valid":
        assert "faulted_valid" in text
    elif fault == "reset":
        assert ".reset(1'b0)" in text
    else:
        assert "faulted_y" not in text
        assert "faulted_valid" not in text


def test_bounded_script_forces_one_reset_cycle() -> None:
    script = probe.emit_bounded_script(
        top="miter",
        cycles=8,
        expect_counterexample=False,
    )

    assert "sat -verify -seq 8" in script
    assert "-set-init-def" in script
    assert "-prove-skip 1" in script
    assert "-set-at 1 reset 1" in script
    assert "-set-at 8 reset 0" in script
    assert "async2sync" in script


def test_negative_bounded_script_does_not_use_verify() -> None:
    script = probe.emit_bounded_script(
        top="miter",
        cycles=4,
        expect_counterexample=True,
    )

    assert "sat -seq 4" in script
    assert "sat -verify" not in script


def test_inductive_script_is_zero_initialized_and_reset_low() -> None:
    script = probe.emit_inductive_script(top="miter", maxsteps=12)

    assert "-tempinduct" in script
    assert "-set-init-zero" in script
    assert "-set reset 0" in script
    assert "-maxsteps 12" in script


def test_module_parser_rejects_multiple_modules() -> None:
    with pytest.raises(probe.PostPhysicalError, match="exactly one module"):
        probe._extract_module_name(
            "module one(input a); endmodule\nmodule two(input b); endmodule\n",
            context="fixture",
        )


def test_resolve_under_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(probe.PostPhysicalError, match="escapes"):
        probe._resolve_under(root, "../outside.txt", context="fixture")


def test_physical_prerequisite_rejects_premature_equivalence_claim(
    tmp_path: Path,
) -> None:
    (tmp_path / "evidence").mkdir()
    (tmp_path / "prepared" / "registered").mkdir(parents=True)
    prepared = {
        "schema": "hephaestus.openroad-physical-prepared.v1",
        "backends": {name: {} for name in probe._BACKENDS},
    }
    prepared_path = tmp_path / "prepared" / "prepared.json"
    prepared_path.write_text(
        json.dumps(prepared, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prepared" / "registered" / "reference_core.sv").write_text(
        "module reference_core(input x, output y); assign y=x; endmodule\n",
        encoding="utf-8",
    )
    physical = {
        "schema": "hephaestus.openroad-physical-evidence.v1",
        "backends": {name: {} for name in probe._BACKENDS},
        "source": {"prepared_manifest_sha256": probe._sha256(prepared_path)},
        "claims": {
            "registered_source_binding_verified": True,
            "pinned_orfs_image_used": True,
            "all_three_backends_placed": True,
            "all_three_backends_routed": True,
            "all_three_backends_emitted_gds": True,
            "all_three_backends_emitted_spef": True,
            "two_attempts_per_backend_completed": True,
            "physical_repeatability_verified": True,
            "common_physical_boundary_verified": True,
            "post_physical_equivalence_verified": True,
        },
    }
    (tmp_path / "evidence" / "openroad_physical_evidence.json").write_text(
        json.dumps(physical, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(probe.PostPhysicalError, match="unexpectedly claims"):
        probe._validate_physical_evidence(tmp_path)
