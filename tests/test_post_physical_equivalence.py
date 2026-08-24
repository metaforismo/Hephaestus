from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import hephaestus.post_physical_equivalence as ppe
from hephaestus.post_physical_equivalence import (
    _builder,
    _common,
    _proof,
    _reference,
    _source,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _physical_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "pinned_orfs_image_used": True,
        "all_three_backends_placed": True,
        "all_three_backends_routed": True,
        "all_three_backends_emitted_gds": True,
        "all_three_backends_emitted_spef": True,
        "two_attempts_per_backend_completed": True,
        "physical_repeatability_verified": True,
        "physical_metrics_recorded": True,
        "common_physical_boundary_verified": True,
        "post_physical_equivalence_verified": False,
        "comparative_ppa_claim_enabled": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _registered_claims() -> dict[str, bool]:
    return {
        "source_matched_integer_contract_verified": True,
        "source_exhaustive_combinational_equivalence_verified": True,
        "source_formal_negative_control_counterexample_found": True,
        "registered_streaming_interface_generated": True,
        "registered_backends_match_oracle_on_executed_schedule": True,
        "one_cycle_latency_verified_on_executed_schedule": True,
        "initiation_interval_one_verified_on_executed_schedule": True,
        "reset_flush_verified_on_executed_schedule": True,
        "simulation_negative_control_detected": True,
        "sequential_formal_equivalence_verified": False,
        "post_synthesis_ppa_measured": False,
        "placement_performed": False,
        "routing_performed": False,
        "power_estimated": False,
        "post_layout_pex_verified": False,
        "silicon_verified": False,
    }


def _run_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "pinned_orfs_image_used": True,
        "placement_performed": True,
        "routing_performed": True,
        "gds_generated": True,
        "spef_generated": True,
        "metadata_generated": True,
        "post_physical_equivalence_verified": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _qualified_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "both_physical_attempts_per_backend_bound": True,
        "all_three_routed_registered_implementations_equivalent": True,
        "data_corruption_negative_control_detected": True,
        "valid_latency_negative_control_detected": True,
        "reset_state_negative_control_detected": True,
        "post_physical_equivalence_verified": True,
        "comparative_ppa_claim_enabled": True,
        "four_state_semantics_verified": False,
        "timing_annotated_functional_semantics_verified": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _proof_contract() -> dict[str, object]:
    return {
        "method": [
            "equiv_make",
            "equiv_miter",
            "bounded_sat_reset_base_case",
            "equiv_struct",
            "equiv_simple",
            "equiv_induct",
        ],
        "bounded_reset_cycles": 5,
        "bounded_reset_prove_skip": 1,
        "bounded_reset_sequence": [1, 0, 0, 0, 0],
        "equiv_induct_sequence_length": 4,
        "attempts_per_backend": 2,
        "positive_base_cases_per_backend": 2,
        "positive_induction_proofs_per_backend": 2,
        "negative_controls": ["data", "valid", "reset"],
        "semantics": "two-state zero-delay clock-edge functional sequential equivalence",
        "reset_model": (
            "source synchronous active-high reset; routed reset normalized with async2sync; "
            "arbitrary asynchronous between-edge reset events are excluded"
        ),
    }


def _make_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    root = tmp_path / "physical"
    registered_root = root / "prepared" / "registered"
    registered_root.mkdir(parents=True)
    models = tmp_path / "models.v"
    models.write_text(
        "module sg13g2_buf_1(input A, output X); assign X = A; endmodule\n",
        encoding="utf-8",
    )

    registered_backends: dict[str, object] = {}
    prepared_backends: dict[str, object] = {}
    source_values: dict[str, object] = {}
    for backend in _common._BACKENDS:
        top = f"tile_{backend}"
        core_name = f"{backend}_core.sv"
        wrapper_name = f"{backend}_registered.sv"
        core = registered_root / core_name
        wrapper = registered_root / wrapper_name
        core.write_text(
            f"module core_{backend}(input [1:0] x_flat, output [1:0] y_flat);\n"
            "  assign y_flat = x_flat;\n"
            "endmodule\n",
            encoding="utf-8",
        )
        wrapper.write_text(
            f"module {top}(input clk, input reset, input valid_in, "
            "input [1:0] x_flat, output reg valid_out, output reg [1:0] y_flat);\n"
            "  always @(posedge clk) begin\n"
            "    if (reset) begin valid_out <= 0; y_flat <= 0; end\n"
            "    else begin valid_out <= valid_in; y_flat <= x_flat; end\n"
            "  end\n"
            "endmodule\n",
            encoding="utf-8",
        )
        registered_backends[backend] = {
            "core_module": f"core_{backend}",
            "core_rtl": core_name,
            "core_sha256": _sha256(core),
            "wrapper_module": top,
            "wrapper_rtl": wrapper_name,
            "wrapper_sha256": _sha256(wrapper),
            "runtime_coefficient_reads_per_matvec": 0,
        }
        prepared_backends[backend] = dict(registered_backends[backend])
        source_values[backend] = {
            "top": top,
            "core_sha256": _sha256(core),
            "wrapper_sha256": _sha256(wrapper),
        }

    registered = {
        "schema": "hephaestus.registered-matched-tiles.v1",
        "contract": {
            "clock_edge": "rising",
            "reset_style": "synchronous_active_high",
            "latency_cycles": 1,
            "valid_latency_cycles": 1,
            "initiation_interval_cycles": 1,
            "input_bits": 2,
            "output_bits": 2,
        },
        "claims": _registered_claims(),
        "backends": registered_backends,
    }
    registered_path = registered_root / "registered_manifest.json"
    _write_json(registered_path, registered)

    prepared = {
        "schema": "hephaestus.openroad-physical-prepared.v1",
        "backends": prepared_backends,
    }
    prepared_path = root / "prepared" / "prepared.json"
    _write_json(prepared_path, prepared)

    physical_backends: dict[str, object] = {}
    for backend in _common._BACKENDS:
        backend_runs = []
        routed_digest = None
        for attempt in (1, 2):
            attempt_root = root / "downloaded-runs" / f"openroad-physical-run-{backend}-{attempt}"
            routed_path = attempt_root / "results" / backend / f"attempt-{attempt}" / "6_final.v"
            routed_path.parent.mkdir(parents=True)
            routed_path.write_text(
                f"module tile_{backend}(input clk, input reset, input valid_in, "
                "input [1:0] x_flat, output valid_out, output [1:0] y_flat);\n"
                "  assign valid_out = valid_in;\n"
                "  assign y_flat = x_flat;\n"
                "endmodule\n",
                encoding="utf-8",
            )
            routed_digest = _sha256(routed_path)
            routed_meta = {
                "path": str(routed_path.relative_to(attempt_root)),
                "sha256": routed_digest,
                "size_bytes": routed_path.stat().st_size,
            }
            run_manifest = {
                "schema": "hephaestus.openroad-physical-run.v1",
                "identity": {"backend": backend, "attempt": attempt},
                "source": {
                    "prepared_manifest_sha256": _sha256(prepared_path),
                    "registered_manifest_sha256": _sha256(registered_path),
                    "core_sha256": source_values[backend]["core_sha256"],
                    "wrapper_sha256": source_values[backend]["wrapper_sha256"],
                },
                "claims": _run_claims(),
                "artifacts": {"final_verilog": routed_meta},
            }
            original_manifest = attempt_root / "openroad_run.json"
            _write_json(original_manifest, run_manifest)
            manifest_name = f"run_manifests/{backend}-attempt-{attempt:02d}.json"
            bound_manifest = root / "evidence" / manifest_name
            _write_json(bound_manifest, run_manifest)
            backend_runs.append(
                {
                    "attempt": attempt,
                    "manifest": manifest_name,
                    "manifest_sha256": _sha256(bound_manifest),
                    "artifacts": {"final_verilog": routed_meta},
                }
            )
        physical_backends[backend] = {
            "core_sha256": source_values[backend]["core_sha256"],
            "wrapper_sha256": source_values[backend]["wrapper_sha256"],
            "repeatability": {"passed": True},
            "runs": backend_runs,
            "routed_sha256": routed_digest,
        }

    physical = {
        "schema": "hephaestus.openroad-physical-evidence.v1",
        "evidence_level": "matched_registered_orfs_rtl_to_gds_repeatability",
        "source": {
            "prepared_manifest_sha256": _sha256(prepared_path),
            "registered_manifest_sha256": _sha256(registered_path),
        },
        "claims": _physical_claims(),
        "backends": physical_backends,
    }
    _write_json(root / "evidence" / "openroad_physical_evidence.json", physical)
    return root, models, tmp_path / "reference.json", source_values


def _write_reference(
    path: Path,
    models: Path,
    source_values: dict[str, object],
    *,
    mismatch: bool = False,
) -> None:
    root = path.parent / "physical"
    physical = json.loads(
        (root / "evidence" / "openroad_physical_evidence.json").read_text(encoding="utf-8")
    )
    backends: dict[str, object] = {}
    negative_counts = {"data": 1, "valid": 1, "reset": 49}
    for backend in _common._BACKENDS:
        source = source_values[backend]
        top = source["top"]
        routed_digest = physical["backends"][backend]["runs"][0]["artifacts"]["final_verilog"][
            "sha256"
        ]
        gate_wrappers = []
        base_cases = []
        inductions = []
        for attempt in (1, 2):
            gate_top = f"{top}_routed_attempt_{attempt}"
            gate_wrapper = ppe.emit_passthrough_wrapper(
                routed_top=top,
                wrapper_top=gate_top,
                input_bits=2,
                output_bits=2,
            )
            base_script = ppe.emit_bounded_reset_script(
                source_top=top,
                gate_top=gate_top,
                expect_counterexample=False,
            )
            induction_script = ppe.emit_equivalence_script(
                source_top=top,
                gate_top=gate_top,
            )
            gate_wrappers.append(_sha256_text(gate_wrapper))
            base_cases.append(
                {
                    "script_sha256": _sha256_text(base_script),
                    "equiv_cells_total": 49,
                    "proof_success": True,
                }
            )
            inductions.append(
                {
                    "script_sha256": _sha256_text(induction_script),
                    "equiv_cells_total": 49,
                    "equiv_cells_proven": 49,
                    "equiv_cells_unproven": 0,
                }
            )
        controls: dict[str, object] = {}
        for fault in _common._FAULTS:
            gate_top = f"{top}_negative_{fault}"
            wrapper = ppe.emit_fault_wrapper(
                routed_top=top,
                wrapper_top=gate_top,
                input_bits=2,
                output_bits=2,
                fault=fault,
            )
            base_script = ppe.emit_bounded_reset_script(
                source_top=top,
                gate_top=gate_top,
                expect_counterexample=True,
            )
            induction_script = ppe.emit_equivalence_script(
                source_top=top,
                gate_top=gate_top,
            )
            controls[fault] = {
                "wrapper_sha256": _sha256_text(wrapper),
                "reset_synchronized_base_case": {
                    "script_sha256": _sha256_text(base_script),
                    "equiv_cells_total": 49,
                    "counterexample_found": True,
                },
                "steady_state_induction": {
                    "script_sha256": _sha256_text(induction_script),
                    "negative_unproven_cells": negative_counts[fault],
                },
            }
        backends[backend] = {
            "source_core_sha256": source["core_sha256"],
            "source_wrapper_sha256": source["wrapper_sha256"],
            "routed_verilog_sha256": [routed_digest, routed_digest],
            "gate_wrapper_sha256": gate_wrappers,
            "reset_synchronized_base_case": base_cases,
            "steady_state_induction": inductions,
            "negative_controls": controls,
        }

    if mismatch:
        backends["shared_dag"]["steady_state_induction"][0]["equiv_cells_total"] = 48
    reference = {
        "schema": "hephaestus.post-physical-equivalence-reference.v1",
        "reference_id": "ihp-sg13g2-post-physical-equivalence-tiny-v1",
        "stable_projection": {
            "reference_id": "ihp-sg13g2-post-physical-equivalence-tiny-v1",
            "proof_contract": _proof_contract(),
            "functional_cell_models_sha256": _sha256(models),
            "yosys_version": "Yosys 0.33 (git sha1 2584903a060)",
            "backends": backends,
            "claims": _qualified_claims(),
        },
    }
    _write_json(path, reference)


def _mock_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_builder.shutil, "which", lambda _: "/usr/bin/yosys")

    def capture_version(_: str, output: Path) -> dict[str, object]:
        version = "Yosys 0.33 (git sha1 2584903a060)"
        path = output / "yosys.version.txt"
        path.write_text(version + "\n", encoding="utf-8")
        return {
            "executable": "/usr/bin/yosys",
            "version": version,
            "version_file": path.name,
            "version_file_sha256": _sha256(path),
        }

    def run_bounded(
        _: str,
        workdir: Path,
        script: Path,
        *,
        timeout: int,
        expect_counterexample: bool,
    ) -> dict[str, object]:
        assert timeout == 300
        stdout = workdir / f"{script.stem}.stdout.txt"
        stderr = workdir / f"{script.stem}.stderr.txt"
        stdout.write_text("mock bounded proof log\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return {
            "passed": True,
            "expected_counterexample": expect_counterexample,
            "returncode": 0,
            "timed_out": False,
            "sat_pass_started": True,
            "equiv_cells_total": 49,
            "proof_success": not expect_counterexample,
            "counterexample_found": expect_counterexample,
            "cycles": 5,
            "prove_skip": 1,
            "reset_sequence": [1, 0, 0, 0, 0],
            "stdout": stdout.name,
            "stdout_sha256": _sha256(stdout),
            "stderr": stderr.name,
            "stderr_sha256": _sha256(stderr),
        }

    def run_induction(
        _: str,
        workdir: Path,
        script: Path,
        *,
        timeout: int,
        expect_equivalent: bool,
    ) -> dict[str, object]:
        assert timeout == 300
        stdout = workdir / f"{script.stem}.stdout.txt"
        stderr = workdir / f"{script.stem}.stderr.txt"
        stdout.write_text("mock induction log\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        if expect_equivalent:
            return {
                "passed": True,
                "expected_equivalent": True,
                "returncode": 0,
                "timed_out": False,
                "equivalence_success_marker_found": True,
                "equiv_cells_total": 49,
                "equiv_cells_proven": 49,
                "equiv_cells_unproven": 0,
                "negative_unproven_cells": None,
                "induction_step_reached": 2,
                "stdout": stdout.name,
                "stdout_sha256": _sha256(stdout),
                "stderr": stderr.name,
                "stderr_sha256": _sha256(stderr),
            }
        counts = {"data": 1, "valid": 1, "reset": 49}
        return {
            "passed": True,
            "expected_equivalent": False,
            "returncode": 1,
            "timed_out": False,
            "equivalence_success_marker_found": False,
            "equiv_cells_total": None,
            "equiv_cells_proven": None,
            "equiv_cells_unproven": None,
            "negative_unproven_cells": counts[workdir.name],
            "induction_step_reached": 4,
            "stdout": stdout.name,
            "stdout_sha256": _sha256(stdout),
            "stderr": stderr.name,
            "stderr_sha256": _sha256(stderr),
        }

    monkeypatch.setattr(_builder, "_capture_yosys_version", capture_version)
    monkeypatch.setattr(_builder, "_run_bounded_yosys", run_bounded)
    monkeypatch.setattr(_builder, "_run_yosys", run_induction)


def test_bounded_reset_script_establishes_induction_base_case() -> None:
    script = ppe.emit_bounded_reset_script(
        source_top="source",
        gate_top="gate",
        expect_counterexample=False,
    )

    assert "equiv_make gold gate equiv" in script
    assert "equiv_miter -assert reset_miter" in script
    assert "sat -verify -seq 5" in script
    assert "-prove-skip 1" in script
    assert "-prove-asserts" in script
    assert "-set-at 1 reset 1" in script
    assert "-set-at 5 reset 0" in script


def test_equivalence_script_uses_steady_state_induction() -> None:
    script = ppe.emit_equivalence_script(source_top="source", gate_top="gate")

    assert "equiv_make gold gate equiv" in script
    assert "equiv_struct -maxiter 20" in script
    assert "equiv_simple" in script
    assert "equiv_induct -seq 4" in script
    assert "equiv_status -assert" in script
    assert "sat " not in script


@pytest.mark.parametrize("fault", ["data", "valid", "reset"])
def test_fault_wrappers_are_independent(fault: str) -> None:
    wrapper = ppe.emit_fault_wrapper(
        routed_top="routed",
        wrapper_top=f"fault_{fault}",
        input_bits=8,
        output_bits=8,
        fault=fault,
    )

    if fault == "data":
        assert "assign y_flat = routed_y ^ 8'd1;" in wrapper
    elif fault == "valid":
        assert "reg delayed_valid;" in wrapper
        assert "assign valid_out = delayed_valid;" in wrapper
    else:
        assert ".reset(1'b0)" in wrapper


def test_bounded_parser_accepts_proof_and_counterexample(tmp_path: Path) -> None:
    script = tmp_path / "proof.ys"
    script.write_text("# fixture\n", encoding="utf-8")
    positive = tmp_path / "positive-yosys"
    positive.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "Found 49 $equiv cells in equiv:\n"
        "  Of those cells 0 are proven and 49 are unproven.\n"
        "Executing SAT pass.\n"
        "SAT proof finished - no model found: SUCCESS!\n"
        "EOF\n",
        encoding="utf-8",
    )
    positive.chmod(0o755)
    result = _proof._run_bounded_yosys(
        str(positive),
        tmp_path,
        script,
        timeout=30,
        expect_counterexample=False,
    )
    assert result["passed"] is True
    assert result["equiv_cells_total"] == 49

    negative = tmp_path / "negative-yosys"
    negative.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "Found 49 $equiv cells in equiv:\n"
        "  Of those cells 0 are proven and 49 are unproven.\n"
        "Executing SAT pass.\n"
        "SAT proof finished - model found: FAIL!\n"
        "EOF\n",
        encoding="utf-8",
    )
    negative.chmod(0o755)
    result = _proof._run_bounded_yosys(
        str(negative),
        tmp_path,
        script,
        timeout=30,
        expect_counterexample=True,
    )
    assert result["passed"] is True
    assert result["counterexample_found"] is True


def test_yosys_result_parser_accepts_only_final_equiv_status(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yosys"
    executable.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "Proving induction step 1.\n"
        "Proof for induction step failed.\n"
        "Proving induction step 2.\n"
        "Proof for induction step holds. Entire workset of 49 cells proven!\n"
        "Executing EQUIV_STATUS pass.\n"
        "Found 49 $equiv cells in equiv:\n"
        "  Of those cells 49 are proven and 0 are unproven.\n"
        "  Equivalence successfully proven!\n"
        "EOF\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = tmp_path / "proof.ys"
    script.write_text("# fixture\n", encoding="utf-8")

    result = _proof._run_yosys(
        str(executable),
        tmp_path,
        script,
        timeout=30,
        expect_equivalent=True,
    )

    assert result["passed"] is True
    assert result["equiv_cells_total"] == 49
    assert result["equiv_cells_proven"] == 49
    assert result["equiv_cells_unproven"] == 0
    assert result["induction_step_reached"] == 2


def test_build_evidence_binds_six_attempts_and_both_obligations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, models, reference, source_values = _make_fixture(tmp_path)
    _write_reference(reference, models, source_values)
    _mock_tools(monkeypatch)

    evidence = ppe.build_evidence(
        root,
        models,
        reference,
        tmp_path / "out",
        source_revision="1" * 40,
    )

    assert evidence["claims"]["post_physical_equivalence_verified"] is True
    assert evidence["claims"]["comparative_ppa_claim_enabled"] is True
    assert evidence["regression"]["passed"] is True
    assert sum(len(value["attempts"]) for value in evidence["backends"].values()) == 6
    assert sum(len(value["negative_controls"]) for value in evidence["backends"].values()) == 9
    assert all(
        attempt["reset_synchronized_base_case"]["passed"]
        and attempt["steady_state_induction"]["passed"]
        for backend in evidence["backends"].values()
        for attempt in backend["attempts"]
    )
    assert all(
        control["reset_synchronized_base_case"]["passed"]
        and control["steady_state_induction"]["passed"]
        for backend in evidence["backends"].values()
        for control in backend["negative_controls"].values()
    )


def test_mutated_second_run_manifest_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, models, reference, source_values = _make_fixture(tmp_path)
    _write_reference(reference, models, source_values)
    _mock_tools(monkeypatch)
    path = root / "downloaded-runs" / "openroad-physical-run-shared_dag-2" / "openroad_run.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["source"]["wrapper_sha256"] = "0" * 64
    _write_json(path, value)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="digest mismatch"):
        ppe.build_evidence(root, models, reference, tmp_path / "out")


def test_reference_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, models, reference, source_values = _make_fixture(tmp_path)
    _write_reference(reference, models, source_values, mismatch=True)
    _mock_tools(monkeypatch)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="stable projection differs"):
        ppe.build_evidence(root, models, reference, tmp_path / "out")


def test_physical_source_cannot_preclaim_downstream_equivalence(tmp_path: Path) -> None:
    root, _, _, _ = _make_fixture(tmp_path)
    path = root / "evidence" / "openroad_physical_evidence.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["claims"]["post_physical_equivalence_verified"] = True
    _write_json(path, value)

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="claim boundary is invalid"):
        _source._validate_source_chain(root)


def test_resolve_under_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="escapes"):
        _common._resolve_under(root, "../outside.v", context="fixture")


def test_source_revision_must_be_exact_git_sha() -> None:
    with pytest.raises(ppe.PostPhysicalEquivalenceError, match="40-character Git SHA"):
        _reference._execution_context("abc")
