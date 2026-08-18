"""Bind OpenSTA timing results to exhaustive proofs of the exact mapped netlists."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from .report import sha256_file, write_json


class OpenSTABindingError(RuntimeError):
    """Raised when timing and formal evidence cannot be bound safely."""


_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_FORMAL_REQUIRED_CLAIMS = (
    "abc_area_delay_source_evidence_verified",
    "all_abc_sweep_mapped_netlists_equivalent",
    "all_pareto_mapped_netlists_equivalent",
    "mapped_gate_level_equivalence_verified",
    "exhaustive_combinational_equivalence_verified",
    "negative_control_counterexample_found",
)
_TIMING_REQUIRED_CLAIMS = (
    "opensta_binary_built_from_pinned_source",
    "sdc_constraints_applied",
    "setup_checks_passed",
    "detailed_max_path_reported",
    "pre_layout_timing_analyzed",
    "repeatability_verified",
)
_TIMING_FALSE_CLAIMS = (
    "signoff_sta_performed",
    "timing_closed",
    "parasitics_annotated",
    "placement_performed",
    "routing_performed",
    "power_estimated",
    "post_layout_pex_verified",
    "silicon_verified",
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenSTABindingError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OpenSTABindingError(f"JSON artifact must be an object: {path}")
    return value


def _require_claims(
    claims: Any,
    *,
    true_claims: tuple[str, ...],
    false_claims: tuple[str, ...] = (),
    context: str,
) -> dict[str, Any]:
    if not isinstance(claims, dict):
        raise OpenSTABindingError(f"{context} claims are malformed")
    missing_true = [name for name in true_claims if claims.get(name) is not True]
    incorrect_false = [name for name in false_claims if claims.get(name) is not False]
    if missing_true:
        raise OpenSTABindingError(
            f"{context} is missing required true claims: {missing_true}"
        )
    if incorrect_false:
        raise OpenSTABindingError(
            f"{context} must keep these claims false: {incorrect_false}"
        )
    return claims


def _validate_positive_proof(proof: Any, *, context: str) -> None:
    if not isinstance(proof, dict):
        raise OpenSTABindingError(f"{context} proof is malformed")
    result = proof.get("proof")
    if not isinstance(result, dict):
        raise OpenSTABindingError(f"{context}.proof is malformed")
    required = {
        "performed": True,
        "passed": True,
        "proof_success": True,
        "counterexample_found": False,
        "unsupported_cell_error": False,
    }
    if any(result.get(key) is not expected for key, expected in required.items()):
        raise OpenSTABindingError(f"{context} is not a successful positive proof")


def _validate_negative_control(value: Any) -> None:
    if not isinstance(value, dict):
        raise OpenSTABindingError("formal negative control is malformed")
    proof = value.get("proof")
    if not isinstance(proof, dict):
        raise OpenSTABindingError("formal negative-control proof is malformed")
    required = {
        "performed": True,
        "passed": True,
        "proof_success": False,
        "counterexample_found": True,
        "unsupported_cell_error": False,
    }
    if any(proof.get(key) is not expected for key, expected in required.items()):
        raise OpenSTABindingError("formal negative control did not find the required fault")


def _finite_number(value: Any, *, context: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OpenSTABindingError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise OpenSTABindingError(f"{context} must be {qualifier}")
    return result


def build_opensta_formal_binding(
    formal_evidence_path: Path,
    timing_evidence_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Bind every timed netlist to a successful proof of the same mapped-Verilog digest."""

    formal_path = formal_evidence_path.resolve()
    timing_path = timing_evidence_path.resolve()
    formal = _load_json(formal_path)
    timing = _load_json(timing_path)

    if formal.get("schema") != "hephaestus.abc-area-delay-formal-evidence.v1":
        raise OpenSTABindingError("unsupported ABC area-delay formal evidence schema")
    if formal.get("evidence_level") != "yosys_sat_abc_area_delay_mapped_equivalence":
        raise OpenSTABindingError("unsupported ABC area-delay formal evidence level")
    if timing.get("schema") != "hephaestus.opensta-sdc-probe.v1":
        raise OpenSTABindingError("unsupported OpenSTA timing evidence schema")
    if timing.get("evidence_level") != "opensta_sdc_pre_layout_timing_probe":
        raise OpenSTABindingError("unsupported OpenSTA timing evidence level")

    _require_claims(
        formal.get("claims"),
        true_claims=_FORMAL_REQUIRED_CLAIMS,
        context="formal evidence",
    )
    _require_claims(
        timing.get("claims"),
        true_claims=_TIMING_REQUIRED_CLAIMS,
        false_claims=_TIMING_FALSE_CLAIMS,
        context="timing evidence",
    )
    _validate_negative_control(formal.get("negative_control"))

    formal_backends = formal.get("backends")
    timing_results = timing.get("results")
    if not isinstance(formal_backends, dict) or set(formal_backends) != set(_BACKENDS):
        raise OpenSTABindingError("formal evidence does not contain the three matched backends")
    if not isinstance(timing_results, list) or not timing_results:
        raise OpenSTABindingError("timing evidence contains no results")

    expected_pairs: set[tuple[str, str]] = set()
    for backend_name in _BACKENDS:
        backend = formal_backends[backend_name]
        if not isinstance(backend, dict):
            raise OpenSTABindingError(f"formal backend {backend_name!r} is malformed")
        if backend.get("all_pareto_runs_covered") is not True:
            raise OpenSTABindingError(f"formal backend {backend_name!r} misses Pareto proofs")
        pareto_labels = backend.get("pareto_labels")
        if not isinstance(pareto_labels, list) or not pareto_labels:
            raise OpenSTABindingError(f"formal backend {backend_name!r} has no Pareto labels")
        expected_pairs.update((backend_name, str(label)) for label in pareto_labels)

    if len(timing_results) != len(expected_pairs):
        raise OpenSTABindingError(
            "timing result count differs from the formally proved Pareto-netlist count"
        )

    bound_results: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_digests: set[str] = set()
    for raw_result in timing_results:
        if not isinstance(raw_result, dict):
            raise OpenSTABindingError("timing result entry is malformed")
        backend_name = raw_result.get("backend")
        label = raw_result.get("label")
        if not isinstance(backend_name, str) or not isinstance(label, str):
            raise OpenSTABindingError("timing result is missing its backend or label")
        pair = (backend_name, label)
        if pair not in expected_pairs or pair in seen_pairs:
            raise OpenSTABindingError(f"unexpected or duplicate timing result: {pair}")
        seen_pairs.add(pair)

        formal_backend = formal_backends[backend_name]
        runs = formal_backend.get("runs")
        proofs = formal_backend.get("proofs")
        if not isinstance(runs, dict) or not isinstance(proofs, dict):
            raise OpenSTABindingError(f"formal backend {backend_name!r} is malformed")
        formal_run = runs.get(label)
        if not isinstance(formal_run, dict) or formal_run.get("equivalence_verified") is not True:
            raise OpenSTABindingError(f"timed netlist {backend_name}/{label} was not proved")

        digest = raw_result.get("mapped_verilog_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise OpenSTABindingError(f"timing result {backend_name}/{label} has no valid digest")
        if formal_run.get("mapped_verilog_sha256") != digest:
            raise OpenSTABindingError(
                f"timed netlist digest differs from formal proof for {backend_name}/{label}"
            )
        if digest in seen_digests:
            raise OpenSTABindingError(
                f"distinct timing results unexpectedly reuse mapped digest {digest}"
            )
        seen_digests.add(digest)

        representative = formal_run.get("proof_representative")
        proof = proofs.get(representative) if isinstance(representative, str) else None
        _validate_positive_proof(
            proof,
            context=f"formal proof {backend_name}/{representative}",
        )
        if proof.get("mapped_verilog_sha256") != digest:
            raise OpenSTABindingError(
                f"proof representative digest differs for {backend_name}/{label}"
            )

        if raw_result.get("repeatability_passed") is not True:
            raise OpenSTABindingError(f"timing result {backend_name}/{label} is not repeatable")
        attempts = raw_result.get("attempts")
        if type(attempts) is not int or attempts < 2:
            raise OpenSTABindingError(f"timing result {backend_name}/{label} has too few attempts")
        result_timing = raw_result.get("timing")
        if not isinstance(result_timing, dict) or result_timing.get("returncode") != 0:
            raise OpenSTABindingError(f"OpenSTA failed for {backend_name}/{label}")

        period = _finite_number(
            result_timing.get("period_ns"),
            context=f"{backend_name}/{label}.period_ns",
            positive=True,
        )
        delay = _finite_number(
            result_timing.get("derived_data_delay_ns"),
            context=f"{backend_name}/{label}.derived_data_delay_ns",
            positive=True,
        )
        slack = _finite_number(
            result_timing.get("worst_slack_ns"),
            context=f"{backend_name}/{label}.worst_slack_ns",
        )
        if not math.isclose(period - slack, delay, rel_tol=0, abs_tol=1e-6):
            raise OpenSTABindingError(
                f"period-minus-slack differs from reported delay for {backend_name}/{label}"
            )

        bound_results.append(
            {
                "backend": backend_name,
                "label": label,
                "mapped_verilog_sha256": digest,
                "formal_proof_representative": representative,
                "formal_equivalence_verified": True,
                "timing_repeatability_verified": True,
                "attempts": attempts,
                "abc_library_area": raw_result.get("abc_library_area"),
                "abc_delay_picoseconds": raw_result.get("abc_delay_picoseconds"),
                "opensta_data_delay_ns": delay,
                "virtual_clock_period_ns": period,
                "worst_slack_ns": slack,
            }
        )

    if seen_pairs != expected_pairs:
        raise OpenSTABindingError("timing evidence does not cover every formally proved Pareto netlist")

    binding = {
        "schema": "hephaestus.opensta-formal-binding.v1",
        "evidence_level": "opensta_pre_layout_timing_of_formally_proved_abc_netlists",
        "source": {
            "formal_evidence": formal_path.name,
            "formal_evidence_sha256": sha256_file(formal_path),
            "timing_evidence": timing_path.name,
            "timing_evidence_sha256": sha256_file(timing_path),
        },
        "scope": {
            "backends": list(_BACKENDS),
            "formally_proved_timed_netlists": len(bound_results),
            "unique_mapped_verilog_digests": len(seen_digests),
            "combinational": True,
            "sequential_depth": 0,
            "parasitics_annotated": False,
        },
        "technology": formal.get("technology"),
        "timing_contract": timing.get("assumptions"),
        "tool": timing.get("tool"),
        "results": sorted(bound_results, key=lambda item: (item["backend"], item["label"])),
        "claims": {
            "all_timed_netlists_formally_equivalent": True,
            "formal_negative_control_counterexample_found": True,
            "opensta_pre_layout_timing_analyzed": True,
            "timing_repeatability_verified": True,
            "signoff_sta_performed": False,
            "timing_closed": False,
            "parasitics_annotated": False,
            "placement_performed": False,
            "routing_performed": False,
            "power_estimated": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    output = output_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, binding)
    return binding


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind OpenSTA timing results to exhaustive proofs of the exact netlists."
    )
    parser.add_argument("formal_evidence", type=Path)
    parser.add_argument("timing_evidence", type=Path)
    parser.add_argument("--out", type=Path, default=Path("build/opensta-formal-binding.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = build_opensta_formal_binding(
            arguments.formal_evidence,
            arguments.timing_evidence,
            arguments.out,
        )
    except (OpenSTABindingError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        "bound "
        f"{result['scope']['formally_proved_timed_netlists']} formally proved timing results"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
