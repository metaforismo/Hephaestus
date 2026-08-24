from __future__ import annotations

from pathlib import Path

import pytest

import hephaestus.post_physical_equivalence as ppe
from hephaestus.post_physical_equivalence import _common, _proof, _reference


def _fake_yosys(tmp_path: Path, *, include_sat_banner: bool) -> Path:
    executable = tmp_path / "fake-yosys"
    seeds = "\n".join(f"Seed $equiv cell: cell_{index}" for index in range(49))
    banner = (
        "Executing SAT pass (solving SAT problems in the circuit).\n" if include_sat_banner else ""
    )
    executable.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        f"{seeds}\n"
        f"{banner}"
        "SAT proof finished - no model found: SUCCESS!\n"
        "EOF\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "proof.ys"
    script.write_text("# fixture\n", encoding="utf-8")
    return script


def _negative_projection(unproven_cells: int) -> dict[str, object]:
    return {
        "backends": {
            backend: {
                "negative_controls": {
                    fault: {
                        "steady_state_induction": {
                            "negative_unproven_cells": unproven_cells,
                            "script_sha256": "a" * 64,
                        }
                    }
                    for fault in _common._FAULTS
                }
            }
            for backend in _common._BACKENDS
        }
    }


def test_bounded_parser_accepts_the_pinned_yosys_033_banner(tmp_path: Path) -> None:
    result = _proof._run_bounded_yosys(
        str(_fake_yosys(tmp_path, include_sat_banner=True)),
        tmp_path,
        _script(tmp_path),
        timeout=30,
        expect_counterexample=False,
    )

    assert result["passed"] is True
    assert result["sat_pass_started"] is True
    assert result["equiv_cells_total"] == 49
    assert result["miter_seed_cells"] == 49
    assert result["proof_success"] is True


def test_bounded_parser_rejects_a_success_marker_without_a_sat_pass(
    tmp_path: Path,
) -> None:
    result = _proof._run_bounded_yosys(
        str(_fake_yosys(tmp_path, include_sat_banner=False)),
        tmp_path,
        _script(tmp_path),
        timeout=30,
        expect_counterexample=False,
    )

    assert result["passed"] is False
    assert result["sat_pass_started"] is False
    assert result["equiv_cells_total"] == 49
    assert result["proof_success"] is True


def test_negative_reference_pins_detection_not_solver_decomposition() -> None:
    one_unproven = _reference._canonicalize_projection(
        _negative_projection(1),
        context="one",
    )
    all_unproven = _reference._canonicalize_projection(
        _negative_projection(49),
        context="all",
    )

    assert one_unproven == all_unproven
    steady = one_unproven["backends"]["shared_dag"]["negative_controls"]["data"][
        "steady_state_induction"
    ]
    assert steady == {
        "negative_control_passed": True,
        "script_sha256": "a" * 64,
        "unproven_equivalence_detected": True,
    }


def test_negative_reference_rejects_zero_unproven_cells() -> None:
    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="legacy unproven count"):
        _reference._canonicalize_projection(
            _negative_projection(0),
            context="zero",
        )


def test_negative_reference_rejects_contradictory_legacy_predicates() -> None:
    projection = _negative_projection(1)
    projection["backends"]["shared_dag"]["negative_controls"]["data"][
        "steady_state_induction"
    ]["negative_control_passed"] = False

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="contradicts"):
        _reference._canonicalize_projection(projection, context="contradictory")


def test_projection_diagnostics_report_exact_nested_paths() -> None:
    differences = _reference._projection_differences(
        {"backend": {"attempts": [1, 2]}, "claim": True},
        {"backend": {"attempts": [1, 3, 4]}},
    )

    assert differences == [
        "$.backend.attempts.length: expected=2, actual=3",
        "$.backend.attempts[1]: expected=2, actual=3",
        "$.backend.attempts[2]: expected=<missing>, actual=4",
        "$.claim: expected=true, actual=<missing>",
    ]


def test_public_builder_rejects_an_output_ancestor_of_inputs(tmp_path: Path) -> None:
    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="overlaps"):
        ppe.build_evidence(
            tmp_path / "physical",
            tmp_path / "models.v",
            tmp_path / "reference.json",
            tmp_path,
        )


def test_public_builder_preserves_an_existing_unrelated_output(tmp_path: Path) -> None:
    output = tmp_path / "existing-output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="already exists"):
        ppe.build_evidence(
            tmp_path / "physical",
            tmp_path / "models.v",
            tmp_path / "reference.json",
            output,
        )

    assert sentinel.read_text(encoding="utf-8") == "do not delete\n"
