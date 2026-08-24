"""Permanent same-run post-physical equivalence evidence builder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ._common import (
    _BACKENDS,
    _EQUIV_SEQUENCE_LENGTH,
    _EVIDENCE_LEVEL,
    _FAULTS,
    _SCHEMA,
    PostPhysicalEquivalenceError,
    _copy_bound,
    _load_json,
    _require_claims,
    _require_digest,
    _safe_module,
    _sha256,
    _verify_file,
    _write_json,
)
from ._proof import (
    _capture_yosys_version,
    _run_yosys,
    emit_equivalence_script,
    emit_fault_wrapper,
    emit_passthrough_wrapper,
)
from ._reference import (
    _execution_context,
    _load_reference,
    _validate_reference,
)
from ._source import _validate_source_chain


def build_evidence(
    physical_root: Path,
    models_path: Path,
    reference_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    timeout: int = 300,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Prove both routed attempts for every backend and bind the result."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    root = physical_root.resolve()
    models = models_path.resolve()
    reference_file = reference_path.resolve()
    output = output_dir.resolve()
    if output == root or root in output.parents:
        raise PostPhysicalEquivalenceError("output directory must not be inside the input artifact")
    if models.is_symlink() or not models.is_file() or models.stat().st_size == 0:
        raise PostPhysicalEquivalenceError(f"functional cell models are invalid: {models}")
    if reference_file.is_symlink() or not reference_file.is_file():
        raise PostPhysicalEquivalenceError(f"regression reference is invalid: {reference_file}")
    resolved_yosys = shutil.which(yosys)
    if resolved_yosys is None:
        raise PostPhysicalEquivalenceError(f"Yosys executable was not found: {yosys}")

    chain = _validate_source_chain(root)
    reference = _load_reference(reference_file)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    toolchain = _capture_yosys_version(resolved_yosys, output)
    model_digest = _sha256(models)
    reference_digest = _sha256(reference_file)

    source_dir = output / "source"
    source_dir.mkdir()
    source_files = (
        (chain["physical_path"], "openroad_physical_evidence.json"),
        (chain["prepared_path"], "prepared.json"),
        (chain["registered_path"], "registered_manifest.json"),
        (models, "ihp_sg13g2_formal_models.v"),
        (reference_file, "regression_reference.json"),
    )
    for source, name in source_files:
        _copy_bound(
            source,
            source_dir / name,
            expected_digest=_sha256(source),
            context=name,
        )

    evidence: dict[str, Any] = {
        "schema": _SCHEMA,
        "evidence_level": _EVIDENCE_LEVEL,
        "execution": _execution_context(source_revision),
        "source": {
            "physical_evidence_sha256": _sha256(chain["physical_path"]),
            "prepared_manifest_sha256": _sha256(chain["prepared_path"]),
            "registered_manifest_sha256": _sha256(chain["registered_path"]),
            "functional_cell_models_sha256": model_digest,
            "regression_reference_sha256": reference_digest,
        },
        "toolchain": toolchain,
        "proof_contract": {
            "method": ["equiv_make", "equiv_struct", "equiv_simple", "equiv_induct"],
            "equiv_induct_sequence_length": _EQUIV_SEQUENCE_LENGTH,
            "attempts_per_backend": 2,
            "positive_proofs_per_backend": 2,
            "negative_controls": list(_FAULTS),
            "semantics": "two-state functional sequential equivalence",
            "reset_model": "synchronous active-high source and routed reset",
        },
        "backends": {},
    }

    physical = chain["physical"]
    prepared = chain["prepared"]
    input_bits = chain["input_bits"]
    output_bits = chain["output_bits"]
    registered_root = root / "prepared" / "registered"

    for backend in _BACKENDS:
        prepared_backend = prepared["backends"][backend]
        physical_backend = physical["backends"][backend]
        if physical_backend.get("repeatability", {}).get("passed") is not True:
            raise PostPhysicalEquivalenceError(f"{backend} physical repeatability is not qualified")
        source_core = _verify_file(
            registered_root,
            prepared_backend.get("core_rtl"),
            prepared_backend.get("core_sha256"),
            context=f"{backend} source core",
        )
        source_wrapper = _verify_file(
            registered_root,
            prepared_backend.get("wrapper_rtl"),
            prepared_backend.get("wrapper_sha256"),
            context=f"{backend} source wrapper",
        )
        source_top = _safe_module(
            prepared_backend.get("wrapper_module"),
            context=f"{backend} source wrapper module",
        )
        if physical_backend.get("core_sha256") != prepared_backend.get("core_sha256"):
            raise PostPhysicalEquivalenceError(f"{backend} physical core binding differs")
        if physical_backend.get("wrapper_sha256") != prepared_backend.get("wrapper_sha256"):
            raise PostPhysicalEquivalenceError(f"{backend} physical wrapper binding differs")

        backend_dir = output / "backends" / backend
        backend_dir.mkdir(parents=True)
        core_digest = _sha256(source_core)
        wrapper_digest = _sha256(source_wrapper)
        _copy_bound(
            source_core,
            backend_dir / "source_core.sv",
            expected_digest=core_digest,
            context=f"{backend} source core",
        )
        _copy_bound(
            source_wrapper,
            backend_dir / "source_wrapper.sv",
            expected_digest=wrapper_digest,
            context=f"{backend} source wrapper",
        )
        _copy_bound(
            models,
            backend_dir / "models.v",
            expected_digest=model_digest,
            context="functional cell models",
        )

        runs = physical_backend.get("runs")
        if not isinstance(runs, list) or len(runs) != 2:
            raise PostPhysicalEquivalenceError(f"{backend} must have exactly two physical attempts")
        attempts: list[dict[str, Any]] = []
        routed_digests: list[str] = []
        for attempt in (1, 2):
            run = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            if run is None:
                raise PostPhysicalEquivalenceError(f"{backend} attempt {attempt} is missing")
            manifest_digest = _require_digest(
                run.get("manifest_sha256"),
                context=f"{backend} attempt {attempt} manifest",
            )
            bound_manifest = _verify_file(
                root / "evidence",
                run.get("manifest"),
                manifest_digest,
                context=f"{backend} attempt {attempt} bound manifest",
            )
            attempt_root = (
                root
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            )
            original_manifest = _verify_file(
                attempt_root,
                "openroad_run.json",
                manifest_digest,
                context=f"{backend} attempt {attempt} original manifest",
            )
            if _sha256(bound_manifest) != _sha256(original_manifest):
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} manifest copies differ"
                )
            run_manifest = _load_json(bound_manifest)
            if run_manifest.get("schema") != "hephaestus.openroad-physical-run.v1":
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} has an unsupported run-manifest schema"
                )
            identity = run_manifest.get("identity")
            if not isinstance(identity, dict):
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} run identity is malformed"
                )
            if identity.get("backend") != backend or identity.get("attempt") != attempt:
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} run identity differs"
                )
            run_source = run_manifest.get("source")
            if not isinstance(run_source, dict):
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} run source binding is malformed"
                )
            expected_source = {
                "prepared_manifest_sha256": _sha256(chain["prepared_path"]),
                "registered_manifest_sha256": _sha256(chain["registered_path"]),
                "core_sha256": core_digest,
                "wrapper_sha256": wrapper_digest,
            }
            for field, expected_value in expected_source.items():
                if run_source.get(field) != expected_value:
                    raise PostPhysicalEquivalenceError(
                        f"{backend} attempt {attempt} run {field} differs"
                    )
            _require_claims(
                run_manifest.get("claims"),
                required_true=(
                    "registered_source_binding_verified",
                    "pinned_orfs_image_used",
                    "placement_performed",
                    "routing_performed",
                    "gds_generated",
                    "spef_generated",
                    "metadata_generated",
                ),
                required_false=(
                    "post_physical_equivalence_verified",
                    "drc_clean",
                    "lvs_clean",
                    "power_estimated_with_activity",
                    "post_layout_pex_verified",
                    "foundry_signoff_complete",
                    "silicon_verified",
                ),
                context=f"{backend} attempt {attempt} run",
            )
            routed_meta = run.get("artifacts", {}).get("final_verilog")
            if not isinstance(routed_meta, dict):
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} routed-Verilog metadata is missing"
                )
            if run_manifest.get("artifacts", {}).get("final_verilog") != routed_meta:
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} routed metadata differs from its manifest"
                )
            routed = _verify_file(
                attempt_root,
                routed_meta.get("path"),
                routed_meta.get("sha256"),
                context=f"{backend} attempt {attempt} routed Verilog",
            )
            routed_digest = _sha256(routed)
            routed_digests.append(routed_digest)

            attempt_dir = backend_dir / f"attempt-{attempt}"
            attempt_dir.mkdir()
            _copy_bound(
                routed,
                attempt_dir / "routed.v",
                expected_digest=routed_digest,
                context=f"{backend} attempt {attempt} routed Verilog",
            )
            _copy_bound(
                bound_manifest,
                attempt_dir / "openroad_run.json",
                expected_digest=manifest_digest,
                context=f"{backend} attempt {attempt} run manifest",
            )
            gate_top = f"{source_top}_routed_attempt_{attempt}"
            wrapper_text = emit_passthrough_wrapper(
                routed_top=source_top,
                wrapper_top=gate_top,
                input_bits=input_bits,
                output_bits=output_bits,
            )
            wrapper_path = attempt_dir / "gate_wrapper.sv"
            wrapper_path.write_text(wrapper_text, encoding="utf-8")
            script_text = emit_equivalence_script(
                source_top=source_top,
                gate_top=gate_top,
            )
            script_path = attempt_dir / "positive.ys"
            script_path.write_text(script_text, encoding="utf-8")
            for name in ("source_core.sv", "source_wrapper.sv", "models.v"):
                shutil.copyfile(backend_dir / name, attempt_dir / name)
            result = _run_yosys(
                resolved_yosys,
                attempt_dir,
                script_path,
                timeout=timeout,
                expect_equivalent=True,
            )
            if not result["passed"]:
                raise PostPhysicalEquivalenceError(
                    f"{backend} attempt {attempt} routed equivalence did not prove"
                )
            attempts.append(
                {
                    "attempt": attempt,
                    "physical_run_manifest": {
                        "sha256": manifest_digest,
                    },
                    "routed_verilog": {
                        "sha256": routed_digest,
                    },
                    "proof": {
                        "script": script_path.name,
                        "script_sha256": _sha256(script_path),
                        **result,
                    },
                }
            )

        if len(set(routed_digests)) != 1:
            raise PostPhysicalEquivalenceError(
                f"{backend} physical attempts do not share one routed netlist digest"
            )

        controls: dict[str, Any] = {}
        control_routed = backend_dir / "attempt-1" / "routed.v"
        for fault in _FAULTS:
            control_dir = backend_dir / "negative-controls" / fault
            control_dir.mkdir(parents=True)
            for name in ("source_core.sv", "source_wrapper.sv", "models.v"):
                shutil.copyfile(backend_dir / name, control_dir / name)
            _copy_bound(
                control_routed,
                control_dir / "routed.v",
                expected_digest=routed_digests[0],
                context=f"{backend} negative-control routed Verilog",
            )
            gate_top = f"{source_top}_negative_{fault}"
            wrapper_text = emit_fault_wrapper(
                routed_top=source_top,
                wrapper_top=gate_top,
                input_bits=input_bits,
                output_bits=output_bits,
                fault=fault,
            )
            wrapper_path = control_dir / "gate_wrapper.sv"
            wrapper_path.write_text(wrapper_text, encoding="utf-8")
            script_path = control_dir / "negative.ys"
            script_path.write_text(
                emit_equivalence_script(source_top=source_top, gate_top=gate_top),
                encoding="utf-8",
            )
            result = _run_yosys(
                resolved_yosys,
                control_dir,
                script_path,
                timeout=timeout,
                expect_equivalent=False,
            )
            if not result["passed"]:
                raise PostPhysicalEquivalenceError(
                    f"{backend} {fault} negative control was not detected"
                )
            controls[fault] = {
                "wrapper_sha256": _sha256(wrapper_path),
                "script_sha256": _sha256(script_path),
                "routed_verilog_sha256": routed_digests[0],
                "result": result,
            }

        evidence["backends"][backend] = {
            "source_top": source_top,
            "source_core": {"sha256": core_digest},
            "source_wrapper": {"sha256": wrapper_digest},
            "both_physical_attempts_bound": True,
            "attempts": attempts,
            "negative_controls": controls,
            "passed": True,
        }

    evidence["claims"] = {
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
    evidence["regression"] = _validate_reference(evidence, reference)
    evidence_path = output / "post_physical_equivalence_evidence.json"
    _write_json(evidence_path, evidence)
    summary = [
        "# Qualified post-physical equivalence",
        "",
        f"- source revision: `{evidence['execution']['source_revision']}`",
        "- backends: `3`",
        "- physical attempts bound: `6`",
        "- positive sequential proofs: `6`",
        "- negative controls: `9`",
        "- post-physical equivalence verified: `true`",
        "- comparative microcase PPA claim enabled: `true`",
        "- four-state or timing-annotated semantics: `false`",
        "- DRC / LVS / PEX / power / sign-off / silicon: `false`",
        "",
    ]
    (output / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    return evidence
