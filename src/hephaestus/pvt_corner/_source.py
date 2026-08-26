"""Validation of the exact physical, formal, PDK, Liberty, and tool chain."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ._common import (
    ANALYSIS_REPLAYS,
    BACKENDS,
    CONTRACT_ID,
    CONTRACT_SCHEMA,
    CORNERS,
    PHYSICAL_ATTEMPTS,
    PVTCornerError,
    load_json,
    require_claims,
    require_finite_number,
    require_git_sha,
    require_positive_int,
    require_sha256,
    require_string,
    resolve_input_directory,
    resolve_input_file,
    resolve_under,
    sha256_file,
    verify_file,
)


def validate_contract(path: Path) -> dict[str, Any]:
    contract = load_json(path)
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise PVTCornerError("unsupported PVT contract schema")
    if contract.get("contract_id") != CONTRACT_ID:
        raise PVTCornerError("unexpected PVT contract identity")
    if contract.get("backends") != list(BACKENDS):
        raise PVTCornerError("PVT contract backend order changed")
    if contract.get("corner_order") != list(CORNERS):
        raise PVTCornerError("PVT contract corner order changed")
    if contract.get("physical_attempts") != list(PHYSICAL_ATTEMPTS):
        raise PVTCornerError("PVT contract physical-attempt set changed")
    if contract.get("analysis_replays") != list(ANALYSIS_REPLAYS):
        raise PVTCornerError("PVT contract analysis-replay set changed")
    timeout = require_positive_int(
        contract.get("timeout_seconds"),
        context="PVT timeout_seconds",
    )
    if timeout > 3600:
        raise PVTCornerError("PVT timeout_seconds exceeds the bounded contract")
    negative_period = require_finite_number(
        contract.get("negative_control_clock_period_ns"),
        context="PVT negative-control period",
    )
    if negative_period <= 0:
        raise PVTCornerError("PVT negative-control period must be positive")

    pdk = contract.get("ihp_open_pdk")
    if not isinstance(pdk, dict):
        raise PVTCornerError("PVT PDK contract is malformed")
    require_string(pdk.get("repository"), context="PDK repository")
    require_git_sha(pdk.get("commit"), context="PDK commit")
    liberty = pdk.get("liberty")
    if not isinstance(liberty, dict) or set(liberty) != set(CORNERS):
        raise PVTCornerError("PVT Liberty corner contract is malformed")
    seen_paths: set[str] = set()
    seen_sha256: set[str] = set()
    for label in CORNERS:
        value = liberty[label]
        if not isinstance(value, dict):
            raise PVTCornerError(f"PVT Liberty corner {label} is malformed")
        relative = require_string(value.get("path"), context=f"{label} Liberty path")
        raw = Path(relative)
        if raw.is_absolute() or ".." in raw.parts:
            raise PVTCornerError(f"{label} Liberty path is unsafe")
        if relative in seen_paths:
            raise PVTCornerError("PVT Liberty paths must be distinct")
        seen_paths.add(relative)
        require_git_sha(value.get("git_blob_sha"), context=f"{label} Liberty blob")
        digest = require_sha256(value.get("sha256"), context=f"{label} Liberty")
        if digest in seen_sha256:
            raise PVTCornerError("PVT Liberty SHA-256 digests must be distinct")
        seen_sha256.add(digest)
        voltage = require_finite_number(
            value.get("nominal_voltage_v"),
            context=f"{label} nominal voltage",
        )
        temperature = require_finite_number(
            value.get("nominal_temperature_c"),
            context=f"{label} nominal temperature",
        )
        if voltage <= 0 or not -273.15 < temperature < 1000:
            raise PVTCornerError(f"{label} nominal operating point is invalid")

    opensta = contract.get("opensta")
    if not isinstance(opensta, dict):
        raise PVTCornerError("PVT OpenSTA contract is malformed")
    if opensta.get("repository") != "parallaxsw/OpenSTA":
        raise PVTCornerError("PVT OpenSTA repository changed")
    require_git_sha(opensta.get("commit"), context="OpenSTA commit")

    claim_boundary = contract.get("claim_boundary")
    required_false = (
        "ocv_analyzed",
        "aocv_analyzed",
        "pocv_analyzed",
        "statistical_variation_analyzed",
        "crosstalk_delay_analyzed",
        "ir_drop_analyzed",
        "electromigration_analyzed",
        "thermal_analyzed",
        "foundry_signoff_sta_performed",
        "foundry_signoff_complete",
        "silicon_verified",
    )
    if (
        not isinstance(claim_boundary, dict)
        or set(claim_boundary) != set(required_false)
    ):
        raise PVTCornerError(
            "PVT contract claim boundary must contain exactly the supported false claims"
        )
    require_claims(
        claim_boundary,
        required_true=(),
        required_false=required_false,
        context="PVT contract",
    )
    return contract


def _git_value(root: Path, *args: str, context: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise PVTCornerError(f"cannot determine {context} below {root}")
    return value


def _validate_pdk(
    pdk_root: Path,
    contract: dict[str, Any],
) -> dict[str, Any]:
    actual_commit = require_git_sha(
        _git_value(pdk_root, "rev-parse", "HEAD", context="PDK commit"),
        context="actual PDK commit",
    )
    expected_commit = contract["ihp_open_pdk"]["commit"]
    if actual_commit != expected_commit:
        raise PVTCornerError(
            f"PDK checkout differs: expected {expected_commit}, got {actual_commit}"
        )
    liberties: dict[str, Any] = {}
    for label in CORNERS:
        specification = contract["ihp_open_pdk"]["liberty"][label]
        path = verify_file(
            pdk_root,
            specification["path"],
            specification["sha256"],
            context=f"{label} Liberty",
        )
        actual_blob = require_git_sha(
            _git_value(
                pdk_root,
                "hash-object",
                str(path.relative_to(pdk_root)),
                context=f"{label} Liberty Git blob",
            ),
            context=f"actual {label} Liberty blob",
        )
        if actual_blob != specification["git_blob_sha"]:
            raise PVTCornerError(
                f"{label} Liberty Git blob differs: "
                f"expected {specification['git_blob_sha']}, got {actual_blob}"
            )
        liberties[label] = {
            "path": path,
            "relative_path": path.relative_to(pdk_root).as_posix(),
            "sha256": sha256_file(path),
            "git_blob_sha": actual_blob,
            "nominal_voltage_v": specification["nominal_voltage_v"],
            "nominal_temperature_c": specification["nominal_temperature_c"],
        }
    return {
        "root": pdk_root,
        "commit": actual_commit,
        "liberties": liberties,
    }


def _validate_opensta(
    executable: Path,
    manifest_path: Path,
    contract: dict[str, Any],
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("schema") != "hephaestus.opensta-tool.v1":
        raise PVTCornerError("unsupported OpenSTA tool manifest schema")
    if manifest.get("repository") != contract["opensta"]["repository"]:
        raise PVTCornerError("OpenSTA repository differs from the PVT contract")
    commit = require_git_sha(manifest.get("commit"), context="OpenSTA tool commit")
    if commit != contract["opensta"]["commit"]:
        raise PVTCornerError("OpenSTA commit differs from the PVT contract")
    binary_name = require_string(manifest.get("binary"), context="OpenSTA binary name")
    if binary_name != executable.name:
        raise PVTCornerError("OpenSTA binary name differs from its tool manifest")
    binary_sha = require_sha256(
        manifest.get("binary_sha256"),
        context="OpenSTA binary",
    )
    if sha256_file(executable) != binary_sha:
        raise PVTCornerError("OpenSTA binary digest differs from its tool manifest")
    banner = require_string(manifest.get("banner"), context="OpenSTA banner")
    if not banner.startswith("OpenSTA "):
        raise PVTCornerError("OpenSTA manifest lacks a recognizable banner")
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
        "executable": executable,
        "binary_sha256": binary_sha,
        "commit": commit,
        "banner": banner,
    }


def _validate_physical_claims(value: dict[str, Any]) -> None:
    if value.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise PVTCornerError("unsupported physical evidence schema")
    if value.get("evidence_level") != "matched_registered_orfs_rtl_to_gds_repeatability":
        raise PVTCornerError("unexpected physical evidence level")
    require_claims(
        value.get("claims"),
        required_true=(
            "registered_source_binding_verified",
            "pinned_orfs_image_used",
            "all_three_backends_placed",
            "all_three_backends_routed",
            "all_three_backends_emitted_spef",
            "two_attempts_per_backend_completed",
            "physical_repeatability_verified",
            "common_physical_boundary_verified",
        ),
        required_false=(
            "post_physical_equivalence_verified",
            "comparative_ppa_claim_enabled",
            "drc_clean",
            "lvs_clean",
            "power_estimated_with_activity",
            "post_layout_pex_verified",
            "foundry_signoff_complete",
            "silicon_verified",
        ),
        context="physical evidence",
    )


def _validate_post_claims(value: dict[str, Any]) -> None:
    if value.get("schema") != "hephaestus.post-physical-equivalence-evidence.v1":
        raise PVTCornerError("unsupported post-physical evidence schema")
    if value.get("evidence_level" != (
        "exact_registered_source_to_routed_sequential_equivalence"
    ):
        raise PVTCornerError("unexpected post-physical evidence level")
    require_claims(
        value.get("claims"),
        required_true=(
            "registered_source_binding_verified",
            "both_physical_attempts_per_backend_bound",
            "all_three_routed_registered_implementations_equivalent",
            "data_corruption_negative_control_detected",
            "valid_latency_negative_control_detected",
            "reset_state_negative_control_detected",
            "post_physical_equivalence_verified",
            "comparative_ppa_claim_enabled",
        ),
        required_false=(
            "four_state_semantics_verified",
            "timing_annotated_functional_semantics_verified",
            "drc_clean",
            "lvs_clean",
            "power_estimated_with_activity",
            "post_layout_pex_verified",
            "foundry_signoff_complete",
            "silicon_verified",
        ),
        context="post-physical evidence",
    )
    if value.get("regression", {}).get("passed") is not True:
        raise PVTCornerError("post-physical regression prerequisite did not pass")


def validate_source_chain(
    physical_root: Path,
    post_physical_root: Path,
    pdk_root: Path,
    opensta_executable: Path,
    opensta_manifest_path: Path,
    contract_path: Path,
    *,
    source_revision: str,
) -> dict[str, Any]:
    physical_root = resolve_input_directory(
        physical_root,
        context="physical evidence artifact",
    )
    post_physical_root = resolve_input_directory(
        post_physical_root,
        context="post-physical evidence artifact",
    )
    pdk_root = resolve_input_directory(pdk_root, context="IHP Open PDK checkout")
    opensta_executable = resolve_input_file(
        opensta_executable,
        context="OpenSTA executable",
    )
    opensta_manifest_path = resolve_input_file(
        opensta_manifest_path,
        context="OpenSTA tool manifest",
    )
    contract_path = resolve_input_file(contract_path, context="PVT contract")
    source_revision = require_git_sha(source_revision, context="source revision")
    contract = validate_contract(contract_path)

    physical_path = resolve_under(
        physical_root,
        "evidence/openroad_physical_evidence.json",
        context="physical evidence manifest",
    )
    prepared_path = resolve_under(
        physical_root,
        "prepared/prepared.json",
        context="prepared physical manifest",
    )
    prepared_copy_path = resolve_under(
        physical_root,
        "evidence/source_prepared.json",
        context="bound prepared physical manifest",
    )
    post_path = resolve_under(
        post_physical_root,
        "post_physical_equivalence_evidence.json",
        context="post-physical evidence manifest",
    )
    post_physical_copy = resolve_under(
        post_physical_root,
        "source/openroad_physical_evidence.json",
        context="post-physical physical-evidence copy",
    )

    physical = load_json(physical_path)
    prepared = load_json(prepared_path)
    post = load_json(post_path)
    _validate_physical_claims(physical)
    _validate_post_claims(post)
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise PVTCornerError("unsupported prepared physical evidence schema")

    physical_digest = sha256_file(physical_path)
    expected_physical = require_sha256(
        post.get("source", {}).get("physical_evidence_sha256"),
        context="post-physical physical evidence",
    )
    if physical_digest != expected_physical or sha256_file(post_physical_copy) != physical_digest:
        raise PVTCornerError("post-physical evidence binds another physical manifest")

    prepared_digest = require_sha256(
        physical.get("source", {}).get("prepared_manifest_sha256"),
        context="physical prepared manifest",
    )
    if (
        sha256_file(prepared_path) != prepared_digest
        or sha256_file(prepared_copy_path) != prepared_digest
        or require_sha256(
            post.get("source", {}).get("prepared_manifest_sha256"),
            context="post-physical prepared manifest",
         )
        != prepared_digest
    ):
        raise PVTCornerError("prepared physical manifest binding differs")

    post_revision = require_git_sha(
        post.get("execution", {}).get("source_revision"),
        context="post-physical source revision",
    )
    if post_revision != source_revision:
        raise PVTCornerError(
            "post-physical evidence was produced from another source revision: "
            f"expected {source_revision}, got {post_revision}"
        )

    physical_backends = physical.get("backends")
    prepared_backends = prepared.get("backends")
    post_backends = post.get("backends")
    for value, context in (
        (physical_backends, "physical"),
        (prepared_backends, "prepared"),
        (post_backends, "post-physical"),
    ):
        if not isinstance(value, dict) or set(value) != set(BACKENDS):
            raise PVTCornerError(f"{context} backend set differs from the PVT contract")

    cases: dict[str, dict[int, Any]] = {}
    for backend in BACKENDS:
        physical_backend = physical_backends[backend]
        if physical_backend.get("repeatability", {}).get("passed") is not True:
            raise PVTCornerError(f"{backend} physical repeatability is not qualified")
        runs = physical_backend.get("runs")
        if not isinstance(runs, list) or len(runs) != len(PHYSICAL_ATTEMPTS):
            raise PVTCornerError(f"{backend} physical attempt set is malformed")
        prepared_backend = prepared_backends[backend]
        top = require_string(
            prepared_backend.get("wrapper_module"),
            context=f"{backend} wrapper module",
        )
        sdc = verify_file(
            physical_root / "prepared",
            prepared_backend.get("sdc"),
            prepared_backend.get("sdc_sha256"),
            context=f"{backend} SDC",
        )
        post_backend = post_backends[backend]
        if not isinstance(post_backend, dict) or post_backend.get("passed") is not True:
            raise PVTCornerError(f"{backend} post-physical proof did not pass")
        if post_backend.get("both_physical_attempts_bound") is not True:
            raise PVTCornerError(f"{backend} post-physical proof lacks both attempts")
        post_attempts = post_backend.get("attempts")
        if not isinstance(post_attempts, list) or len(post_attempts) != len(PHYSICAL_ATTEMPTS):
            raise PVTCornerError(f"{backend} post-physical attempt set is malformed")

        cases[backend] = {}
        for attempt in PHYSICAL_ATTEMPTS:
            run_record = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            post_attempt = next(
                (
                    item
                    for item in post_attempts
                    if isinstance(item, dict) and item.get("attempt") == attempt
                ),
                None,
            )
            if run_record is None or post_attempt is None:
                raise PVTCornerError(f"{backend} attempt {attempt} binding is missing")
            manifest_digest = require_sha256(
                run_record.get("manifest_sha256"),
                context=f"{backend} attempt {attempt} run manifest",
            )
            bound_manifest = verify_file(
                physical_root / "evidence",
                run_record.get("manifest"),
                manifest_digest,
                context=f"{backend} attempt {attempt} bound run manifest",
            )
            run_root = (
                physical_root
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            )
            run_root = resolve_input_directory(
                run_root,
                context=f"{backend} attempt {attempt} physical run",
            )
            original_manifest = verify_file(
                run_root,
                "openroad_run.json",
                manifest_digest,
                context=f"{backend} attempt {attempt} original run manifest",
            )
            if sha256_file(bound_manifest) != sha256_file(original_manifest):
                raise PVTCornerError(f"{backend} attempt {attempt} manifest copies differ")
            manifest = load_json(original_manifest)
            if manifest.get("schema") != "hephaestus.openroad-physical-run.v1":
                raise PVTCornerError(f"{backend} attempt {attempt} run schema changed")
            identity = manifest.get("identity")
            if not isinstance(identity, dict) or identity.get("backend") != backend:
                raise PVTCornerError(f"{backend} attempt {attempt} identity differs")
            if identity.get("attempt") != attempt:
                raise PVTCornerError(f"{backend} attempt {attempt} number differs")
            require_claims(
                manifest.get("claims"),
                required_true=(
                    "registered_source_binding_verified",
                    "pinned_orfs_image_used",
                    "placement_performed",
                    "routing_performed",
                    "spef_generated",
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
                context=f"{backend} attempt {attempt} physical run",
            )
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, dict):
                raise PVTCornerError(f"{backend} attempt {attempt} artifacts are malformed")
            netlist_spec = artifacts.get("final_verilog")
            spef_spec = artifacts.get("final_spef")
            if not isinstance(netlist_spec, dict) or not isinstance(spef_spec, dict):
                raise PVTCornerError(f"{backend} attempt {attempt} timing inputs are missing")
            netlist = verify_file(
                run_root,
                netlist_spec.get("path"),
                netlist_spec.get("sha256"),
                expected_size=netlist_spec.get("size_bytes"),
                context=f"{backend} attempt {attempt} routed Verilog",
            )
            spef = verify_file(
                run_root,
                spef_spec.get("path"),
                spef_spec.get("sha256"),
                expected_size=spef_spec.get("size_bytes"),
                context=f"{backend} attempt {attempt} routed SPEF",
            )
            expected_post_manifest = require_sha256(
                post_attempt.get("physical_run_manifest", {}).get("sha256"),
                context=f"{backend} attempt {attempt} post-physical run manifest",
            )
            expected_post_netlist = require_sha256(
                post_attempt.get("routed_verilog", {}).get("sha256"),
                context=f"{backend} attempt {attempt} post-physical netlist",
            )
            if expected_post_manifest != manifest_digest:
                raise PVTCornerError(
                    f"{backend} attempt {attempt} post-physical manifest binding differs"
                )
            if expected_post_netlist != sha256_file(netlist):
                raise PVTCornerError(
                    f"{backend} attempt {attempt} post-physical netlist binding differs"
                )
            normalized = manifest.get("normalized")
            if not isinstance(normalized, dict):
                raise PVTCornerError(
                    f"{backend} attempt {attempt} normalized artifacts are malformed"
                )
            spef_normalized = require_sha256(
                normalized.get("spef_date_normalized_sha256"),
                context=f"{backend} attempt {attempt} normalized SPEF",
            )
            cases[backend][attempt] = {
                "backend": backend,
                "physical_attempt": attempt,
                "top_module": top,
                "sdc": sdc,
                "sdc_sha256": sha256_file(sdc),
                "run_root": run_root,
                "run_manifest": original_manifest,
                "run_manifest_sha256": manifest_digest,
                "routed_verilog": netlist,
                "routed_verilog_sha256": sha256_file(netlist),
                "routed_spef": spef,
                "routed_spef_sha256": sha256_file(spef),
                "spef_date_normalized_sha256": spef_normalized,
            }

    pdk = _validate_pdk(pdk_root, contract)
    opensta = _validate_opensta(opensta_executable, opensta_manifest_path, contract)
    return {
        "physical_root": physical_root,
        "post_physical_root": post_physical_root,
        "physical": physical,
        "physical_path": physical_path,
        "prepared": prepared,
        "prepared_path": prepared_path,
        "post_physical": post,
        "post_physical_path": post_path,
        "contract": contract,
        "contract_path": contract_path,
        "source_revision": source_revision,
        "cases": cases,
        "pdk": pdk,
        "opensta": opensta,
    }
