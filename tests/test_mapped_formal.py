from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.mapped_formal import (
    MappedFormalError,
    _artifact_entry,
    _mapped_proof_script,
    _resolve_artifact,
    _validate_relative_tool_path,
    build_mapped_formal_evidence,
)


def _write_mapped_manifest(
    root: Path,
    *,
    claims: dict[str, bool] | None = None,
    input_count: int = 2,
    output_count: int = 1,
    input_width: int = 4,
    accumulator_width: int = 7,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mapped_evidence.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.standard-cell-mapped-evidence.v1",
                "contract": {
                    "domain": "quantized_integer_core_before_row_scaling",
                    "input_count": input_count,
                    "output_count": output_count,
                    "input_width": input_width,
                    "accumulator_width": accumulator_width,
                },
                "source": {
                    "matched_manifest": "source_matched_manifest.json",
                    "matched_manifest_sha256": "0" * 64,
                },
                "technology": {
                    "technology_id": "test-technology",
                    "liberty_artifact": {
                        "path": "technology/technology.lib",
                        "sha256": "0" * 64,
                    },
                },
                "backends": {},
                "claims": claims
                or {
                    "matched_integer_contract_verified": True,
                    "standard_cell_mapping_performed": True,
                    "mapped_netlist_structurally_checked": True,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_mapped_proof_script_loads_functional_liberty_and_fails_closed() -> None:
    positive = _mapped_proof_script(
        miter_module="mapped_positive_miter",
        expect_counterexample=False,
    )
    negative = _mapped_proof_script(
        miter_module="mapped_negative_miter",
        expect_counterexample=True,
    )

    assert positive.startswith("read_liberty -ignore_miss_func ../../technology/technology.lib")
    assert "hierarchy -check -top mapped_positive_miter" in positive
    assert "check -assert" in positive
    assert "sat -verify -set-def-inputs" in positive
    assert "sat -set-def-inputs" in negative
    assert "-verify" not in negative
    assert "-prove mismatch 0" in positive


def test_mapped_proof_script_rejects_unsafe_names_and_paths() -> None:
    with pytest.raises(MappedFormalError, match="unsafe"):
        _mapped_proof_script(
            miter_module="top; shell touch escaped",
            expect_counterexample=False,
        )
    with pytest.raises(MappedFormalError, match="unsafe path"):
        _mapped_proof_script(
            miter_module="safe_top",
            liberty_path="technology.lib; shell touch escaped",
            expect_counterexample=False,
        )
    with pytest.raises(MappedFormalError, match="must be relative"):
        _validate_relative_tool_path("/tmp/technology.lib")


def test_artifact_resolution_cannot_escape_the_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inside = bundle / "mapped.v"
    inside.write_text("module mapped; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.v"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _resolve_artifact(bundle, "mapped.v", context="mapped Verilog") == inside.resolve()
    with pytest.raises(MappedFormalError, match="escapes"):
        _resolve_artifact(bundle, "../outside.v", context="mapped Verilog")
    with pytest.raises(MappedFormalError, match="must be relative"):
        _resolve_artifact(bundle, str(outside.resolve()), context="mapped Verilog")


def test_artifact_entry_requires_a_pinned_digest() -> None:
    artifacts = {
        "mapped_verilog": {
            "path": "backends/shared/mapped.v",
            "sha256": "a" * 64,
        }
    }
    assert _artifact_entry(
        artifacts,
        "mapped_verilog",
        context="backend shared",
    ) == ("backends/shared/mapped.v", "a" * 64)

    artifacts["mapped_verilog"]["sha256"] = "main"
    with pytest.raises(MappedFormalError, match="SHA-256"):
        _artifact_entry(
            artifacts,
            "mapped_verilog",
            context="backend shared",
        )


def test_mapped_formal_requires_verified_mapping_claims(tmp_path: Path) -> None:
    bundle = tmp_path / "mapped"
    _write_mapped_manifest(
        bundle,
        claims={
            "matched_integer_contract_verified": True,
            "standard_cell_mapping_performed": True,
            "mapped_netlist_structurally_checked": False,
        },
    )

    with pytest.raises(MappedFormalError, match="required before mapped formal proof"):
        build_mapped_formal_evidence(
            bundle,
            tmp_path / "missing-codes.npy",
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_mapped_formal_enforces_the_input_width_limit(tmp_path: Path) -> None:
    bundle = tmp_path / "mapped"
    _write_mapped_manifest(
        bundle,
        input_count=4,
        input_width=8,
        accumulator_width=12,
    )

    with pytest.raises(MappedFormalError, match="exceeds the configured limit"):
        build_mapped_formal_evidence(
            bundle,
            tmp_path / "missing-codes.npy",
            tmp_path / "out",
            yosys="missing-yosys",
            max_input_bits=16,
        )


def test_mapped_formal_rejects_an_unpinned_matched_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "mapped"
    _write_mapped_manifest(bundle)
    preserved = bundle / "source_matched_manifest.json"
    preserved.write_text("{}\n", encoding="utf-8")

    with pytest.raises(MappedFormalError, match="digest does not match"):
        build_mapped_formal_evidence(
            bundle,
            tmp_path / "missing-codes.npy",
            tmp_path / "out",
            yosys="missing-yosys",
        )


def test_mapped_formal_rejects_non_positive_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_input_bits"):
        build_mapped_formal_evidence(
            tmp_path / "mapped",
            tmp_path / "codes.npy",
            tmp_path / "out",
            max_input_bits=0,
        )
    with pytest.raises(ValueError, match="timeout_seconds"):
        build_mapped_formal_evidence(
            tmp_path / "mapped",
            tmp_path / "codes.npy",
            tmp_path / "out",
            timeout_seconds=0,
        )
