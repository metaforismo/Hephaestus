"""Validation of the registered-source and matched-physical prerequisite chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import (
    _BACKENDS,
    PostPhysicalEquivalenceError,
    _load_json,
    _require_claims,
    _require_digest,
    _require_positive_int,
    _sha256,
)


def _validate_source_chain(root: Path) -> dict[str, Any]:
    evidence_path = root / "evidence" / "openroad_physical_evidence.json"
    prepared_path = root / "prepared" / "prepared.json"
    registered_path = root / "prepared" / "registered" / "registered_manifest.json"
    physical = _load_json(evidence_path)
    prepared = _load_json(prepared_path)
    registered = _load_json(registered_path)

    if physical.get("schema") != "hephaestus.openroad-physical-evidence.v1":
        raise PostPhysicalEquivalenceError("unsupported physical evidence schema")
    if physical.get("evidence_level") != ("matched_registered_orfs_rtl_to_gds_repeatability"):
        raise PostPhysicalEquivalenceError("unexpected physical evidence level")
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise PostPhysicalEquivalenceError("unsupported prepared evidence schema")
    if registered.get("schema") != "hephaestus.registered-matched-tiles.v1":
        raise PostPhysicalEquivalenceError("unsupported registered evidence schema")

    _require_claims(
        physical.get("claims"),
        required_true=(
            "registered_source_binding_verified",
            "pinned_orfs_image_used",
            "all_three_backends_placed",
            "all_three_backends_routed",
            "all_three_backends_emitted_gds",
            "all_three_backends_emitted_spef",
            "two_attempts_per_backend_completed",
            "physical_repeatability_verified",
            "physical_metrics_recorded",
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
    _require_claims(
        registered.get("claims"),
        required_true=(
            "source_matched_integer_contract_verified",
            "source_exhaustive_combinational_equivalence_verified",
            "source_formal_negative_control_counterexample_found",
            "registered_streaming_interface_generated",
            "registered_backends_match_oracle_on_executed_schedule",
            "one_cycle_latency_verified_on_executed_schedule",
            "initiation_interval_one_verified_on_executed_schedule",
            "reset_flush_verified_on_executed_schedule",
            "simulation_negative_control_detected",
        ),
        required_false=(
            "sequential_formal_equivalence_verified",
            "post_synthesis_ppa_measured",
            "placement_performed",
            "routing_performed",
            "power_estimated",
            "post_layout_pex_verified",
            "silicon_verified",
        ),
        context="registered evidence",
    )

    for value, context, path in (
        (
            physical.get("source", {}).get("prepared_manifest_sha256"),
            "prepared manifest",
            prepared_path,
        ),
        (
            physical.get("source", {}).get("registered_manifest_sha256"),
            "registered manifest",
            registered_path,
        ),
    ):
        expected = _require_digest(value, context=f"physical {context}")
        actual = _sha256(path)
        if actual != expected:
            raise PostPhysicalEquivalenceError(
                f"{context} digest differs from physical evidence: "
                f"expected {expected}, got {actual}"
            )

    for value, context in (
        (physical.get("backends"), "physical"),
        (prepared.get("backends"), "prepared"),
        (registered.get("backends"), "registered"),
    ):
        if not isinstance(value, dict) or set(value) != set(_BACKENDS):
            raise PostPhysicalEquivalenceError(
                f"{context} backend set differs from the matched contract"
            )

    contract = registered.get("contract")
    if not isinstance(contract, dict):
        raise PostPhysicalEquivalenceError("registered contract is malformed")
    if contract.get("clock_edge") != "rising":
        raise PostPhysicalEquivalenceError("registered contract must use the rising clock edge")
    if contract.get("reset_style") != "synchronous_active_high":
        raise PostPhysicalEquivalenceError("registered reset contract changed")
    if contract.get("latency_cycles") != 1 or contract.get("valid_latency_cycles") != 1:
        raise PostPhysicalEquivalenceError("registered latency contract changed")
    if contract.get("initiation_interval_cycles") != 1:
        raise PostPhysicalEquivalenceError("registered initiation interval changed")
    input_bits = _require_positive_int(contract.get("input_bits"), context="input_bits")
    output_bits = _require_positive_int(contract.get("output_bits"), context="output_bits")

    return {
        "physical": physical,
        "physical_path": evidence_path,
        "prepared": prepared,
        "prepared_path": prepared_path,
        "registered": registered,
        "registered_path": registered_path,
        "input_bits": input_bits,
        "output_bits": output_bits,
    }
