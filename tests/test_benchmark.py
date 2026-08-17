from __future__ import annotations

from pathlib import Path

import numpy as np

from hephaestus.benchmark import (
    _count_adder_nodes,
    _logical_baseline,
    run_suite,
)


def test_logical_baseline_counts_terms_and_row_adders() -> None:
    codes = np.asarray(
        [
            [1, 0, -1, 0],
            [1, 1, 1, 0],
            [0, 0, 0, 0],
        ],
        dtype=np.int64,
    )

    nonzero, naive_adders, density = _logical_baseline(codes)

    assert nonzero == 5
    assert naive_adders == 3
    assert density == 5 / 12


def test_count_adder_nodes_accepts_explicit_and_binary_schemas() -> None:
    assert _count_adder_nodes({"nodes": [{"op": "add"}, {"op": "sub"}]}) == 2
    assert (
        _count_adder_nodes({"nodes": [{"lhs": "x0", "rhs": "x1"}, {"lhs": "n0", "rhs": "x2"}]}) == 2
    )
    assert _count_adder_nodes({"addition_nodes": [{}, {}, {}]}) == 3


def test_tiny_evidence_suite_runs_end_to_end(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    suite = repository / "benchmarks" / "suites" / "tiny.json"
    output = tmp_path / "evidence"

    result = run_suite(suite, output, repository)

    assert result["schema_version"] == 1
    assert result["evidence_level"] == "algorithmic_and_rtl"
    assert [case["name"] for case in result["cases"]] == [
        "tiny_exact",
        "shared_pairs",
    ]
    assert all(case["claims"]["bit_exact_integer_core_verified"] for case in result["cases"])
    assert all(case["metrics"]["runtime_weight_reads_per_matvec"] == 0 for case in result["cases"])
    assert result["claims"] == {
        "post_synthesis_ppa_measured": False,
        "post_layout_pex_verified": False,
        "silicon_verified": False,
    }
    assert (output / "evidence.json").is_file()
    assert (output / "SUMMARY.md").is_file()
