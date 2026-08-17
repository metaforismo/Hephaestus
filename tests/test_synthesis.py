from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.synthesis import (
    SynthesisError,
    _build_script,
    _netlist_metrics,
    _resolve_bundle_artifact,
    _validate_module_name,
    build_synthesis_evidence,
)


def test_build_script_preserves_the_two_evidence_stages() -> None:
    script = _build_script("hephaestus_top")

    assert "hierarchy -check -top hephaestus_top" in script
    assert script.index("pre_techmap.stat.txt") < script.index("techmap")
    assert script.index("techmap") < script.index("post_techmap.stat.txt")
    assert "write_json pre_techmap.netlist.json" in script
    assert "write_json post_techmap.netlist.json" in script


def test_module_name_rejects_script_injection() -> None:
    assert _validate_module_name("safe_module_0") == "safe_module_0"
    with pytest.raises(SynthesisError, match="unsafe"):
        _validate_module_name("top; delete *")
    with pytest.raises(SynthesisError, match="unsafe"):
        _validate_module_name("top\nwrite_json stolen.json")


def test_bundle_artifact_cannot_escape_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inside = bundle / "input.sv"
    inside.write_text("module input_core; endmodule\n", encoding="utf-8")
    outside = tmp_path / "outside.sv"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")

    assert _resolve_bundle_artifact(bundle, "input.sv") == inside.resolve()
    with pytest.raises(SynthesisError, match="escapes"):
        _resolve_bundle_artifact(bundle, "../outside.sv")
    with pytest.raises(SynthesisError, match="relative"):
        _resolve_bundle_artifact(bundle, str(outside.resolve()))


def test_netlist_metrics_are_normalized_from_yosys_json() -> None:
    netlist = {
        "modules": {
            "top": {
                "ports": {
                    "x": {"direction": "input", "bits": [2, 3]},
                    "y": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "add": {
                        "type": "$add",
                        "connections": {"A": [2], "B": [3], "Y": [5]},
                    },
                    "and": {
                        "type": "$_AND_",
                        "connections": {"A": [5], "B": ["1"], "Y": [4]},
                    },
                },
                "netnames": {
                    "x": {"bits": [2, 3]},
                    "sum": {"bits": [5]},
                    "y": {"bits": [4]},
                },
                "memories": {},
            }
        }
    }

    metrics = _netlist_metrics(netlist, "top")

    assert metrics["cell_count"] == 2
    assert metrics["cell_type_histogram"] == {"$_AND_": 1, "$add": 1}
    assert metrics["generic_internal_cell_count"] == 1
    assert metrics["abstract_operator_cell_count"] == 1
    assert metrics["input_bits"] == 2
    assert metrics["output_bits"] == 1
    assert metrics["unique_signal_bits"] == 4
    assert metrics["cell_connection_bits"] == 6


def test_synthesis_requires_a_verified_matched_contract(tmp_path: Path) -> None:
    bundle = tmp_path / "matched"
    bundle.mkdir()
    (bundle / "matched_manifest.json").write_text(
        json.dumps(
            {
                "schema": "hephaestus.matched-baselines.v1",
                "backends": {},
                "claims": {"matched_integer_contract_verified": False},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SynthesisError, match="must be verified"):
        build_synthesis_evidence(bundle, tmp_path / "out")
