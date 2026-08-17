import json
from pathlib import Path

import numpy as np

from hephaestus.cli import main
from hephaestus.emit_sv import emit_systemverilog
from hephaestus.lower import lower_codes


def test_codegen_has_no_runtime_weight_storage() -> None:
    plan = lower_codes(np.array([[1, -2], [0, 4]], dtype=np.int64), input_width=8)
    rtl = emit_systemverilog(plan, module_name="tiny-core")

    assert "module tiny_core" in rtl
    assert "Runtime weight reads per matrix-vector operation: 0" in rtl
    assert "weight" not in "\n".join(
        line for line in rtl.splitlines() if not line.lstrip().startswith("//")
    ).lower()
    assert "case" not in rtl.lower()


def test_zero_matrix_avoids_zero_width_replication() -> None:
    plan = lower_codes(np.zeros((1, 2), dtype=np.int64), input_width=8)
    rtl = emit_systemverilog(plan, module_name="zero")

    assert "{{0{" not in rtl
    assert "assign sx_0 = x_0;" in rtl


def test_cli_compiles_json_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "weights.json"
    source.write_text("[[1.0, -2.0], [0.25, 0.5]]\n", encoding="utf-8")
    output = tmp_path / "out"

    exit_code = main(
        [
            "compile",
            str(source),
            "--out",
            str(output),
            "--module",
            "tiny",
            "--verify-samples",
            "8",
        ]
    )

    assert exit_code == 0
    assert (output / "tiny.sv").exists()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["topology"]["runtime_weight_reads_per_matvec"] == 0
    assert manifest["claims"]["bit_exact_integer_core_verified"] is True
    assert manifest["source"]["selected_values_sha256"]
    assert manifest["topology"]["max_atom_fanout"] >= 1
