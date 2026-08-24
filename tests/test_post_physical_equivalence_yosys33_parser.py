from __future__ import annotations

from pathlib import Path

from hephaestus.post_physical_equivalence import _proof


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
