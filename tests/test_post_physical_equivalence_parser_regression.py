from __future__ import annotations

from pathlib import Path

from hephaestus.post_physical_equivalence import _proof


def test_bounded_parser_accepts_nonvacuous_miter_without_status_summary(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-yosys"
    seeds = "\n".join(
        f"Seed $equiv cell: cell_{index}" for index in range(49)
    )
    executable.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        f"{seeds}\n"
        "Executing SAT pass.\n"
        "SAT proof finished - no model found: SUCCESS!\n"
        "EOF\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    script = tmp_path / "proof.ys"
    script.write_text("# fixture\n", encoding="utf-8")

    result = _proof._run_bounded_yosys(
        str(executable),
        tmp_path,
        script,
        timeout=30,
        expect_counterexample=False,
    )

    assert result["passed"] is True
    assert result["equiv_cells_total"] == 49
    assert result["miter_seed_cells"] == 49
