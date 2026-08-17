"""Exhaustive equivalence evidence for ABC area-delay mapped netlists."""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .formal import emit_miter_systemverilog, emit_reference_systemverilog
from .lower import required_accumulator_width
from .mapped_formal import (
    MappedFormalError,
    _artifact_manifest,
    _contract_dimensions,
    _inspect_liberty_functions,
    _load_codes,
    _load_json,
    _proof_script,
    _resolve_bundle_artifact,
    _resolve_executable,
    _resolve_hashed_artifact,
    _run_sat,
    _tool_version,
)
from .report import sha256_file, write_json


class AbcAreaDelayFormalError(MappedFormalError):
    """Raised when ABC area-delay formal evidence cannot be produced safely."""


_REQUIRED_SOURCE_CLAIMS = (
    "matched_integer_contract_verified",
    "technology_aware_abc_mapping_performed",
    "declared_input_driver_model_used",
    "declared_output_load_used",
    "abc_internal_timing_estimated",
    "abc_delay_targets_swept",
    "target_attainment_evaluated",
    "mapped_netlist_structurally_checked",
    "post_mapping_library_area_estimated",
    "area_delay_product_computed",
)


def _safe_label(value: str, *, context: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
        raise AbcAreaDelayFormalError(f"{context} contains an unsafe label: {value!r}")
    return value


def _positive_number(value: Any, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AbcAreaDelayFormalError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise AbcAreaDelayFormalError(f"{context} must be finite and positive")
    return number


def _require_source_claims(claims: Any) -> dict[str, Any]:
    if not isinstance(claims, dict):
        raise AbcAreaDelayFormalError("ABC area-delay claims are malformed")
    missing = [claim for claim in _REQUIRED_SOURCE_CLAIMS if claims.get(claim) is not True]
    if missing:
        raise AbcAreaDelayFormalError(
            f"ABC area-delay evidence is missing required true claims: {missing}"
        )
    if claims.get("mapped_gate_level_equivalence_verified") is not False:
        raise AbcAreaDelayFormalError(
            "source ABC evidence must not pre-claim mapped gate-level equivalence"
        )
    return claims


def _repeatability_verified(run: dict[str, Any], *, context: str) -> None:
    repeatability = run.get("repeatability")
    if not isinstance(repeatability, dict):
        raise AbcAreaDelayFormalError(f"{context}.repeatability is malformed")
    if repeatability.get("performed") is not True or repeatability.get("passed") is not True:
        raise AbcAreaDelayFormalError(f"{context} must have a passing repeatability run")
    byte_identical = repeatability.get("byte_identical_artifacts")
    if not isinstance(byte_identical, dict) or not byte_identical:
        raise AbcAreaDelayFormalError(
            f"{context}.repeatability.byte_identical_artifacts is malformed"
        )
    if any(value is not True for value in byte_identical.values()):
        raise AbcAreaDelayFormalError(f"{context} contains a non-identical repeatability artifact")
    if repeatability.get("normalized_metrics_identical") is not True:
        raise AbcAreaDelayFormalError(f"{context} repeatability metrics are not identical")


def _select_proof_representatives(
    runs: dict[str, dict[str, Any]],
    pareto_labels: list[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Map every run to one representative, preferring Pareto labels."""

    if not runs:
        raise AbcAreaDelayFormalError("backend run set must not be empty")
    if not pareto_labels or len(set(pareto_labels)) != len(pareto_labels):
        raise AbcAreaDelayFormalError("pareto_labels must be non-empty and unique")
    missing = sorted(set(pareto_labels) - set(runs))
    if missing:
        raise AbcAreaDelayFormalError(f"Pareto labels are absent from runs: {missing}")

    by_digest: dict[str, list[str]] = defaultdict(list)
    for label, run in runs.items():
        digest = run.get("mapped_verilog_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise AbcAreaDelayFormalError(
                f"run {label!r} does not contain a valid mapped-Verilog digest"
            )
        by_digest[digest].append(label)

    pareto_order = {label: index for index, label in enumerate(pareto_labels)}
    representative_for_label: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for digest in sorted(by_digest):
        labels = sorted(by_digest[digest])
        representative = min(
            labels,
            key=lambda label: (
                0 if label in pareto_order else 1,
                pareto_order.get(label, len(pareto_order)),
                label,
            ),
        )
        aliases[representative] = labels
        for label in labels:
            representative_for_label[label] = representative

    return representative_for_label, aliases


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# ABC area-delay mapped-netlist formal evidence",
        "",
        f"Evidence level: `{manifest['evidence_level']}`",
        "",
        f"Technology: `{manifest['technology']['technology_id']}`",
        "",
        "| Backend | Sweep runs | Unique netlists proved | Pareto labels | Result |",
        "|---|---:|---:|---|:---:|",
    ]
    for backend_name, backend in sorted(manifest["backends"].items()):
        pareto = ", ".join(f"`{label}`" for label in backend["pareto_labels"])
        lines.append(
            f"| `{backend_name}` | {len(backend['runs'])} | "
            f"{len(backend['proofs'])} | {pareto} | proved |"
        )
    lines.extend(
        [
            "",
            "Every distinct mapped-Verilog digest is proved exactly once against an independent "
            "integer reference derived directly from `codes.npy`. Other sweep labels may reuse a "
            "proof only when their mapped Verilog is byte-identical.",
            "",
            "A data-dependent output fault must produce a counterexample.",
            "",
            "This layer proves Boolean combinational behavior. It does not prove ABC delay "
            "accuracy, sign-off STA, SDC timing closure, X/Z behavior, placement, routing, "
            "parasitics, power, PEX, or silicon.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_abc_area_delay_formal_evidence(
    area_delay_bundle: Path,
    codes_path: Path,
    output_dir: Path,
    *,
    yosys: str = "yosys",
    max_input_bits: int = 64,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Prove every distinct ABC area-delay mapped netlist."""

    if max_input_bits <= 0:
        raise ValueError("max_input_bits must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    bundle = area_delay_bundle.resolve()
    source_codes = codes_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    area_delay_path = bundle / "abc_area_delay_evidence.json"
    if not area_delay_path.is_file():
        raise AbcAreaDelayFormalError(f"ABC area-delay bundle is missing {area_delay_path.name}")
    source_manifest = _load_json(area_delay_path)
    if source_manifest.get("schema") != "hephaestus.abc-area-delay-evidence.v1":
        raise AbcAreaDelayFormalError("unsupported ABC area-delay evidence schema")
    if source_manifest.get("evidence_level") != "abc_liberty_area_delay_estimate":
        raise AbcAreaDelayFormalError("unsupported ABC area-delay evidence level")
    source_claims = _require_source_claims(source_manifest.get("claims"))

    source = source_manifest.get("source")
    if not isinstance(source, dict):
        raise AbcAreaDelayFormalError("ABC area-delay source metadata is malformed")
    matched_value = source.get("matched_manifest")
    matched_digest = source.get("matched_manifest_sha256")
    if not isinstance(matched_value, str) or not matched_value:
        raise AbcAreaDelayFormalError(
            "ABC area-delay evidence does not identify its matched manifest"
        )
    matched_path = _resolve_bundle_artifact(bundle, matched_value)
    if (
        not isinstance(matched_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", matched_digest) is None
        or sha256_file(matched_path) != matched_digest
    ):
        raise AbcAreaDelayFormalError(
            "matched manifest digest does not match ABC area-delay evidence"
        )
    matched_manifest = _load_json(matched_path)
    if matched_manifest.get("schema") != "hephaestus.matched-baselines.v1":
        raise AbcAreaDelayFormalError("unsupported matched-baseline manifest schema")
    matched_claims = matched_manifest.get("claims")
    if (
        not isinstance(matched_claims, dict)
        or matched_claims.get("matched_integer_contract_verified") is not True
    ):
        raise AbcAreaDelayFormalError("source matched integer contract is not verified")

    source_contract = source_manifest.get("contract")
    matched_contract = matched_manifest.get("contract")
    if not isinstance(source_contract, dict) or not isinstance(matched_contract, dict):
        raise AbcAreaDelayFormalError("ABC area-delay or matched contract is malformed")
    input_count, output_count, input_width, accumulator_width = _contract_dimensions(
        source_contract,
        context="ABC area-delay contract",
    )
    _contract_dimensions(matched_contract, context="matched contract")
    contract_fields = (
        "domain",
        "input_count",
        "output_count",
        "input_width",
        "accumulator_width",
        "combinational",
        "latency_cycles",
    )
    mismatched_fields = [
        field
        for field in contract_fields
        if source_contract.get(field) != matched_contract.get(field)
    ]
    if mismatched_fields:
        raise AbcAreaDelayFormalError(
            f"ABC area-delay and matched contracts disagree on: {mismatched_fields}"
        )
    if source_contract.get("combinational") is not True:
        raise AbcAreaDelayFormalError(
            "ABC area-delay formal evidence requires a combinational contract"
        )
    if source_contract.get("latency_cycles") != 0:
        raise AbcAreaDelayFormalError("ABC area-delay formal evidence requires zero-cycle latency")

    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    if input_bits > max_input_bits:
        raise AbcAreaDelayFormalError(
            f"formal input width {input_bits} exceeds the configured limit of {max_input_bits} bits"
        )

    if not source_codes.is_file():
        raise AbcAreaDelayFormalError(f"quantized codes do not exist: {source_codes}")
    artifact_hashes = matched_manifest.get("artifact_sha256")
    if not isinstance(artifact_hashes, dict):
        raise AbcAreaDelayFormalError("matched artifact hashes are malformed")
    expected_codes_digest = artifact_hashes.get("source_codes")
    if (
        not isinstance(expected_codes_digest, str)
        or sha256_file(source_codes) != expected_codes_digest
    ):
        raise AbcAreaDelayFormalError("source codes do not match the matched-baseline manifest")
    codes = _load_codes(source_codes)
    if codes.shape != (output_count, input_count):
        raise AbcAreaDelayFormalError(
            f"codes shape {codes.shape} does not match ({output_count}, {input_count})"
        )
    minimum_width = required_accumulator_width(codes, input_width)
    if accumulator_width < minimum_width:
        raise AbcAreaDelayFormalError(
            f"contract accumulator width {accumulator_width} is smaller than "
            f"the required width {minimum_width}"
        )

    technology = source_manifest.get("technology")
    if not isinstance(technology, dict):
        raise AbcAreaDelayFormalError("technology metadata is malformed")
    technology_id = technology.get("technology_id")
    if not isinstance(technology_id, str) or not technology_id:
        raise AbcAreaDelayFormalError("technology ID is missing")
    liberty_source = _resolve_hashed_artifact(
        bundle,
        technology.get("liberty_artifact"),
        context="technology.liberty_artifact",
    )
    technology_config_source = _resolve_hashed_artifact(
        bundle,
        technology.get("configuration_artifact"),
        context="technology.configuration_artifact",
    )
    liberty_metadata, functional_cells = _inspect_liberty_functions(liberty_source)
    technology_library = technology.get("library")
    if not isinstance(technology_library, dict):
        raise AbcAreaDelayFormalError("technology library metadata is malformed")
    if liberty_metadata["sha256"] != technology_library.get("sha256"):
        raise AbcAreaDelayFormalError("proof Liberty digest differs from ABC area-delay evidence")
    if liberty_metadata["library"] != technology_library.get("name"):
        raise AbcAreaDelayFormalError("proof Liberty name differs from ABC area-delay evidence")

    technology_config = _load_json(technology_config_source)
    if not isinstance(technology_config, dict):
        raise AbcAreaDelayFormalError("technology configuration is malformed")
    if technology_config.get("schema") != "hephaestus.technology.v1":
        raise AbcAreaDelayFormalError("unsupported technology configuration schema")
    if technology_config.get("technology_id") != technology_id:
        raise AbcAreaDelayFormalError("technology configuration ID differs from source evidence")

    configuration = source_manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise AbcAreaDelayFormalError("ABC area-delay configuration metadata is malformed")
    evidence_config_source = _resolve_hashed_artifact(
        bundle,
        configuration.get("artifact"),
        context="configuration.artifact",
    )
    constraints_source = _resolve_hashed_artifact(
        bundle,
        configuration.get("constraints_artifact"),
        context="configuration.constraints_artifact",
    )

    preserved_source = output / "source_abc_area_delay_evidence.json"
    preserved_matched = output / "source_matched_manifest.json"
    preserved_codes = output / "source_codes.npy"
    technology_dir = output / "technology"
    configuration_dir = output / "configuration"
    technology_dir.mkdir(parents=True, exist_ok=True)
    configuration_dir.mkdir(parents=True, exist_ok=True)
    preserved_liberty = technology_dir / "technology.lib"
    preserved_technology_config = technology_dir / "technology.json"
    preserved_evidence_config = configuration_dir / "area_delay.json"
    preserved_constraints = configuration_dir / "abc.constr"
    shutil.copyfile(area_delay_path, preserved_source)
    shutil.copyfile(matched_path, preserved_matched)
    shutil.copyfile(source_codes, preserved_codes)
    shutil.copyfile(liberty_source, preserved_liberty)
    shutil.copyfile(technology_config_source, preserved_technology_config)
    shutil.copyfile(evidence_config_source, preserved_evidence_config)
    shutil.copyfile(constraints_source, preserved_constraints)

    reference_module = "hephaestus_abc_area_delay_formal_reference"
    reference_path = output / "reference.sv"
    reference_path.write_text(
        emit_reference_systemverilog(
            codes,
            input_width=input_width,
            accumulator_width=accumulator_width,
            module_name=reference_module,
        ),
        encoding="utf-8",
    )

    source_backends = source_manifest.get("backends")
    matched_backends = matched_manifest.get("backends")
    if not isinstance(source_backends, dict) or not source_backends:
        raise AbcAreaDelayFormalError("ABC area-delay evidence does not contain backends")
    if not isinstance(matched_backends, dict) or not matched_backends:
        raise AbcAreaDelayFormalError("matched manifest does not contain backends")
    if set(source_backends) != set(matched_backends):
        raise AbcAreaDelayFormalError("ABC area-delay and matched backend sets differ")

    resolved_backends: dict[str, dict[str, Any]] = {}
    total_sweep_runs = 0
    for backend_name in sorted(source_backends):
        _safe_label(backend_name, context="backend name")
        backend = source_backends[backend_name]
        matched_backend = matched_backends[backend_name]
        if not isinstance(backend, dict) or not isinstance(matched_backend, dict):
            raise AbcAreaDelayFormalError(f"backend {backend_name!r} metadata is malformed")
        module = backend.get("module")
        if not isinstance(module, str) or module != matched_backend.get("module"):
            raise AbcAreaDelayFormalError(
                f"backend {backend_name!r} module differs from matched manifest"
            )

        raw_runs = backend.get("runs")
        raw_pareto = backend.get("pareto_labels")
        if not isinstance(raw_runs, dict) or not raw_runs:
            raise AbcAreaDelayFormalError(f"backend {backend_name!r} has no sweep runs")
        if not isinstance(raw_pareto, list) or any(
            not isinstance(label, str) for label in raw_pareto
        ):
            raise AbcAreaDelayFormalError(f"backend {backend_name!r} Pareto labels are malformed")
        pareto_labels = [
            _safe_label(label, context=f"backends.{backend_name}.pareto_labels")
            for label in raw_pareto
        ]

        runs: dict[str, dict[str, Any]] = {}
        for label, raw_run in raw_runs.items():
            if not isinstance(label, str):
                raise AbcAreaDelayFormalError(
                    f"backend {backend_name!r} contains a non-string run label"
                )
            safe_label = _safe_label(
                label,
                context=f"backends.{backend_name}.runs",
            )
            if not isinstance(raw_run, dict):
                raise AbcAreaDelayFormalError(f"run {backend_name!r}/{safe_label!r} is malformed")
            if raw_run.get("area_cross_check_passed") is not True:
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} failed its area cross-check"
                )
            if raw_run.get("structural_check") != "yosys check -assert":
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} lacks a fail-closed structural check"
                )
            _repeatability_verified(
                raw_run,
                context=f"backends.{backend_name}.runs.{safe_label}",
            )
            _positive_number(
                raw_run.get("library_area"),
                context=f"{backend_name}/{safe_label}.library_area",
            )
            _positive_number(
                raw_run.get("critical_path_delay_picoseconds"),
                context=f"{backend_name}/{safe_label}.critical_path_delay_picoseconds",
            )

            metrics = raw_run.get("metrics")
            if not isinstance(metrics, dict):
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} metrics are malformed"
                )
            if metrics.get("input_bits") != input_bits or metrics.get("output_bits") != output_bits:
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} widths differ from contract"
                )
            histogram = metrics.get("cell_type_histogram")
            if not isinstance(histogram, dict) or not histogram:
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} has no cell histogram"
                )
            if any(
                not isinstance(cell, str) or not cell or type(count) is not int or count <= 0
                for cell, count in histogram.items()
            ):
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} has invalid cell data"
                )
            cell_count = metrics.get("cell_count")
            if (
                type(cell_count) is not int
                or cell_count <= 0
                or cell_count != sum(histogram.values())
            ):
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} cell count is invalid"
                )
            missing_functions = sorted(set(histogram) - set(functional_cells))
            if missing_functions:
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} uses cells without "
                    f"functional Liberty models: {missing_functions}"
                )

            artifacts = raw_run.get("artifacts")
            if not isinstance(artifacts, dict):
                raise AbcAreaDelayFormalError(
                    f"run {backend_name!r}/{safe_label!r} artifacts are malformed"
                )
            mapped_verilog = _resolve_hashed_artifact(
                bundle,
                artifacts.get("mapped_verilog"),
                context=(f"backends.{backend_name}.runs.{safe_label}.artifacts.mapped_verilog"),
            )
            runs[safe_label] = {
                "target_picoseconds": raw_run.get("target_picoseconds"),
                "target_met": raw_run.get("target_met"),
                "library_area": float(raw_run["library_area"]),
                "critical_path_delay_picoseconds": float(
                    raw_run["critical_path_delay_picoseconds"]
                ),
                "mapped_cell_count": int(cell_count),
                "mapped_cell_types": sorted(histogram),
                "mapped_verilog": mapped_verilog,
                "mapped_verilog_sha256": sha256_file(mapped_verilog),
            }

        representative_for_label, aliases = _select_proof_representatives(
            runs,
            pareto_labels,
        )
        resolved_backends[backend_name] = {
            "module": module,
            "pareto_labels": pareto_labels,
            "runs": runs,
            "representative_for_label": representative_for_label,
            "aliases": aliases,
        }
        total_sweep_runs += len(runs)

    executable = _resolve_executable(yosys)
    version = _tool_version(executable)
    backend_evidence: dict[str, Any] = {}
    unique_proofs = 0

    for backend_index, backend_name in enumerate(sorted(resolved_backends)):
        backend = resolved_backends[backend_name]
        module = backend["module"]
        runs = backend["runs"]
        proofs: dict[str, Any] = {}

        for proof_index, representative in enumerate(sorted(backend["aliases"])):
            run = runs[representative]
            miter_module = f"hephaestus_abc_formal_b{backend_index}_p{proof_index}_miter"
            result = _run_sat(
                source_rtl=run["mapped_verilog"],
                reference_rtl=reference_path,
                miter_text=emit_miter_systemverilog(
                    dut_module=module,
                    reference_module=reference_module,
                    input_bits=input_bits,
                    output_bits=output_bits,
                    module_name=miter_module,
                ),
                miter_module=miter_module,
                run_dir=output / "proofs" / f"{backend_name}__{representative}",
                executable=executable,
                timeout_seconds=timeout_seconds,
                expect_counterexample=False,
            )
            proofs[representative] = {
                "mapped_verilog_sha256": run["mapped_verilog_sha256"],
                "covered_labels": backend["aliases"][representative],
                "proof": {key: value for key, value in result.items() if key != "artifacts"},
                "artifacts": _artifact_manifest(output, result["artifacts"]),
            }
            unique_proofs += 1

        run_evidence: dict[str, Any] = {}
        for label, run in runs.items():
            representative = backend["representative_for_label"][label]
            run_evidence[label] = {
                "target_picoseconds": run["target_picoseconds"],
                "target_met": run["target_met"],
                "library_area": run["library_area"],
                "critical_path_delay_picoseconds": (run["critical_path_delay_picoseconds"]),
                "mapped_cell_count": run["mapped_cell_count"],
                "mapped_verilog_sha256": run["mapped_verilog_sha256"],
                "proof_representative": representative,
                "proof_reused_by_identical_mapped_verilog": label != representative,
                "equivalence_verified": True,
            }

        backend_evidence[backend_name] = {
            "module": module,
            "pareto_labels": backend["pareto_labels"],
            "runs": run_evidence,
            "proofs": proofs,
            "all_sweep_runs_covered": set(run_evidence) == set(backend["representative_for_label"]),
            "all_pareto_runs_covered": set(backend["pareto_labels"]).issubset(run_evidence),
        }

    negative_backend = (
        "shared_dag" if "shared_dag" in resolved_backends else sorted(resolved_backends)[0]
    )
    negative_backend_data = resolved_backends[negative_backend]
    preferred_negative = next(
        (
            label
            for label in negative_backend_data["pareto_labels"]
            if negative_backend_data["representative_for_label"][label] == label
        ),
        sorted(negative_backend_data["aliases"])[0],
    )
    negative_run = negative_backend_data["runs"][preferred_negative]
    negative_miter = "hephaestus_abc_area_delay_formal_negative_miter"
    negative_result = _run_sat(
        source_rtl=negative_run["mapped_verilog"],
        reference_rtl=reference_path,
        miter_text=emit_miter_systemverilog(
            dut_module=negative_backend_data["module"],
            reference_module=reference_module,
            input_bits=input_bits,
            output_bits=output_bits,
            module_name=negative_miter,
            inject_fault=True,
        ),
        miter_module=negative_miter,
        run_dir=output / "proofs" / "negative_control",
        executable=executable,
        timeout_seconds=timeout_seconds,
        expect_counterexample=True,
    )

    manifest = {
        "schema": "hephaestus.abc-area-delay-formal-evidence.v1",
        "evidence_level": "yosys_sat_abc_area_delay_mapped_equivalence",
        "source": {
            "abc_area_delay_evidence": preserved_source.name,
            "abc_area_delay_evidence_sha256": sha256_file(preserved_source),
            "matched_manifest": preserved_matched.name,
            "matched_manifest_sha256": sha256_file(preserved_matched),
            "codes": preserved_codes.name,
            "codes_sha256": sha256_file(preserved_codes),
        },
        "technology": {
            "technology_id": technology_id,
            "configuration": {
                "path": preserved_technology_config.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_technology_config),
            },
            "liberty": {
                "path": preserved_liberty.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_liberty),
                "library": liberty_metadata["library"],
                "cell_declarations": liberty_metadata["cell_declarations"],
                "functional_cell_models": liberty_metadata["functional_cell_models"],
            },
        },
        "configuration": {
            "area_delay": {
                "path": preserved_evidence_config.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_evidence_config),
            },
            "abc_constraints": {
                "path": preserved_constraints.relative_to(output).as_posix(),
                "sha256": sha256_file(preserved_constraints),
            },
        },
        "tool": {
            "name": "Yosys SAT with Liberty functional models",
            "requested_executable": yosys,
            "version": version,
        },
        "scope": {
            "domain": source_contract.get("domain"),
            "input_bits": input_bits,
            "output_bits": output_bits,
            "max_input_bits": max_input_bits,
            "defined_inputs_only": True,
            "combinational": True,
            "sequential_depth": 0,
            "timeout_seconds_per_proof": timeout_seconds,
            "source_sweep_runs": total_sweep_runs,
            "unique_mapped_netlists_proved": unique_proofs,
            "proof_reuse_rule": "only byte-identical mapped-Verilog SHA-256 digests",
            "four_state_semantics": False,
            "timing_semantics": False,
        },
        "reference": {
            "module": reference_module,
            "rtl": reference_path.name,
            "sha256": sha256_file(reference_path),
            "derived_directly_from_codes": True,
            "uses_compilation_plan": False,
            "uses_abc_timing_results": False,
        },
        "flow": {
            "script_template": _proof_script(
                miter_module="TOP",
                expect_counterexample=False,
            ).replace("-top TOP", "-top <miter>"),
            "liberty_mode": ("functional models loaded with read_liberty -ignore_miss_func"),
            "structural_check": "yosys check -assert",
            "proof_engine": "yosys sat",
        },
        "backends": backend_evidence,
        "negative_control": {
            "backend": negative_backend,
            "source_label": preferred_negative,
            "mapped_verilog_sha256": negative_run["mapped_verilog_sha256"],
            "fault": "xor output bit 0 with input bit 0",
            "proof": {key: value for key, value in negative_result.items() if key != "artifacts"},
            "artifacts": _artifact_manifest(output, negative_result["artifacts"]),
        },
        "claims": {
            "matched_integer_contract_verified": True,
            "abc_area_delay_source_evidence_verified": True,
            "technology_aware_abc_mapping_performed": True,
            "mapped_netlist_structurally_checked": True,
            "liberty_functional_models_verified": True,
            "all_abc_sweep_mapped_netlists_equivalent": True,
            "all_pareto_mapped_netlists_equivalent": True,
            "mapped_gate_level_equivalence_verified": True,
            "exhaustive_combinational_equivalence_verified": True,
            "negative_control_counterexample_found": True,
            "abc_internal_timing_estimated": source_claims["abc_internal_timing_estimated"],
            "sequential_equivalence_verified": False,
            "four_state_equivalence_verified": False,
            "signoff_sta_performed": False,
            "sdc_timing_analyzed": False,
            "timing_closed": False,
            "power_estimated": False,
            "placement_performed": False,
            "routing_performed": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "abc_area_delay_formal_evidence.json", manifest)
    _write_summary(output / "SUMMARY.md", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prove every distinct ABC area-delay mapped netlist against an "
            "independent integer reference."
        )
    )
    parser.add_argument("area_delay_bundle", type=Path)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("build/abc-area-delay-formal"),
    )
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--max-input-bits", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_abc_area_delay_formal_evidence(
            arguments.area_delay_bundle,
            arguments.codes,
            arguments.out,
            yosys=arguments.yosys,
            max_input_bits=arguments.max_input_bits,
            timeout_seconds=arguments.timeout,
        )
    except (
        MappedFormalError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"proved {manifest['scope']['unique_mapped_netlists_proved']} "
        "unique ABC-mapped netlists covering "
        f"{manifest['scope']['source_sweep_runs']} sweep runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
