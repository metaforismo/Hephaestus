"""Matched RTL baselines for one compiled constant-matrix artifact."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .emit_sv import sanitize_identifier
from .ir import CompilationPlan
from .lower import required_accumulator_width
from .report import sha256_file, write_json

IntArray = NDArray[np.int64]


class BaselineError(RuntimeError):
    """Raised when matched baselines cannot be generated or verified safely."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read JSON artifact {path}: {exc}") from exc


def _load_codes(path: Path) -> IntArray:
    try:
        values = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise BaselineError(f"cannot read quantized codes {path}: {exc}") from exc
    codes = np.asarray(values, dtype=np.int64)
    if codes.ndim != 2 or codes.size == 0:
        raise BaselineError(f"codes must be a non-empty 2-D matrix, got {codes.shape}")
    return codes


def _signed_literal(width: int, value: int) -> str:
    magnitude = abs(value)
    literal = f"{width}'sd{magnitude}"
    return literal if value >= 0 else f"-{literal}"


def _module_preamble(
    *,
    module_name: str,
    input_count: int,
    output_count: int,
    input_width: int,
    accumulator_width: int,
    description: str,
) -> list[str]:
    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    module = sanitize_identifier(module_name)
    lines = [
        f"// {description}",
        "// Matched contract: combinational quantized integer core before row scaling.",
        f"module {module} (",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        f"    output wire signed [{output_bits - 1}:0] y_flat",
        ");",
        "",
        f"  localparam integer INPUT_COUNT = {input_count};",
        f"  localparam integer OUTPUT_COUNT = {output_count};",
        f"  localparam integer INPUT_WIDTH = {input_width};",
        f"  localparam integer ACC_WIDTH = {accumulator_width};",
        "",
    ]
    extension = accumulator_width - input_width
    for index in range(input_count):
        lines.extend(
            [
                f"  wire signed [{input_width - 1}:0] x_{index};",
                f"  wire signed [{accumulator_width - 1}:0] sx_{index};",
                f"  assign x_{index} = x_flat[{index * input_width} +: INPUT_WIDTH];",
            ]
        )
        if extension == 0:
            lines.append(f"  assign sx_{index} = x_{index};")
        else:
            lines.append(
                f"  assign sx_{index} = {{{{{extension}{{x_{index}[INPUT_WIDTH-1]}}}}, x_{index}}};"
            )
    lines.append("")
    return lines


def emit_constant_multiplier_systemverilog(
    codes: NDArray[np.integer],
    *,
    input_width: int,
    accumulator_width: int,
    module_name: str,
) -> str:
    """Emit one constant multiplication per nonzero coefficient."""

    matrix = np.asarray(codes, dtype=np.int64)
    minimum_width = required_accumulator_width(matrix, input_width)
    if accumulator_width < minimum_width:
        raise ValueError(
            f"accumulator_width={accumulator_width} is unsafe; "
            f"at least {minimum_width} bits are required"
        )

    rows, columns = matrix.shape
    lines = _module_preamble(
        module_name=module_name,
        input_count=columns,
        output_count=rows,
        input_width=input_width,
        accumulator_width=accumulator_width,
        description="Matched baseline using explicit constant multiplication operators.",
    )

    for row_index, row in enumerate(matrix):
        terms: list[str] = []
        for column_index, raw_coefficient in enumerate(row):
            coefficient = int(raw_coefficient)
            if coefficient == 0:
                continue
            term = f"m_o{row_index}_i{column_index}"
            terms.append(term)
            lines.extend(
                [
                    f"  wire signed [{accumulator_width - 1}:0] {term};",
                    (
                        f"  assign {term} = $signed(sx_{column_index}) * "
                        f"{_signed_literal(accumulator_width, coefficient)};"
                    ),
                ]
            )

        expression = "{ACC_WIDTH{1'b0}}" if not terms else " + ".join(terms)
        lines.append(
            f"  assign y_flat[{row_index * accumulator_width} +: ACC_WIDTH] = {expression};"
        )
        lines.append("")

    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def emit_naive_shift_add_systemverilog(
    codes: NDArray[np.integer],
    *,
    input_width: int,
    accumulator_width: int,
    module_name: str,
) -> str:
    """Emit balanced per-output shift/add trees with no cross-output sharing."""

    matrix = np.asarray(codes, dtype=np.int64)
    minimum_width = required_accumulator_width(matrix, input_width)
    if accumulator_width < minimum_width:
        raise ValueError(
            f"accumulator_width={accumulator_width} is unsafe; "
            f"at least {minimum_width} bits are required"
        )

    rows, columns = matrix.shape
    lines = _module_preamble(
        module_name=module_name,
        input_count=columns,
        output_count=rows,
        input_width=input_width,
        accumulator_width=accumulator_width,
        description="Matched baseline using independent balanced shift/add output trees.",
    )

    for row_index, row in enumerate(matrix):
        level: list[str] = []
        for column_index, raw_coefficient in enumerate(row):
            coefficient = int(raw_coefficient)
            if coefficient == 0:
                continue
            magnitude = abs(coefficient)
            if magnitude & (magnitude - 1):
                raise ValueError(f"coefficient {coefficient} is not a signed power of two")
            shift = magnitude.bit_length() - 1
            term = f"a_o{row_index}_i{column_index}"
            source = f"(sx_{column_index} <<< {shift})"
            expression = source if coefficient > 0 else f"-{source}"
            lines.extend(
                [
                    f"  wire signed [{accumulator_width - 1}:0] {term};",
                    f"  assign {term} = {expression};",
                ]
            )
            level.append(term)

        depth = 0
        while len(level) > 1:
            next_level: list[str] = []
            for pair_index in range(0, len(level), 2):
                lhs = level[pair_index]
                if pair_index + 1 == len(level):
                    next_level.append(lhs)
                    continue
                rhs = level[pair_index + 1]
                node = f"n_o{row_index}_d{depth}_p{pair_index // 2}"
                lines.extend(
                    [
                        f"  wire signed [{accumulator_width - 1}:0] {node};",
                        f"  assign {node} = {lhs} + {rhs};",
                    ]
                )
                next_level.append(node)
            level = next_level
            depth += 1

        expression = "{ACC_WIDTH{1'b0}}" if not level else level[0]
        lines.append(
            f"  assign y_flat[{row_index * accumulator_width} +: ACC_WIDTH] = {expression};"
        )
        lines.append("")

    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _pack_lanes(values: list[int], input_width: int) -> int:
    mask = (1 << input_width) - 1
    packed = 0
    for index, value in enumerate(values):
        packed |= (value & mask) << (index * input_width)
    return packed


def _verification_vectors(
    *,
    input_count: int,
    input_width: int,
    random_vectors: int,
    seed: int,
) -> list[int]:
    if random_vectors < 0:
        raise ValueError("random_vectors must be non-negative")
    minimum = -(1 << (input_width - 1))
    maximum = (1 << (input_width - 1)) - 1

    lane_sets: list[list[int]] = [
        [0] * input_count,
        [maximum] * input_count,
        [minimum] * input_count,
        [-1] * input_count,
    ]
    for index in range(input_count):
        positive = [0] * input_count
        positive[index] = maximum
        lane_sets.append(positive)
        negative = [0] * input_count
        negative[index] = minimum
        lane_sets.append(negative)

    vectors = [_pack_lanes(values, input_width) for values in lane_sets]
    rng = random.Random(seed)
    bus_width = input_count * input_width
    vectors.extend(rng.getrandbits(bus_width) for _ in range(random_vectors))
    return list(dict.fromkeys(vectors))


def emit_equivalence_testbench(
    *,
    shared_module: str,
    multiplier_module: str,
    naive_module: str,
    input_count: int,
    output_count: int,
    input_width: int,
    accumulator_width: int,
    random_vectors: int,
    seed: int,
    module_name: str,
) -> tuple[str, int]:
    """Emit a self-checking testbench comparing all matched backends."""

    vectors = _verification_vectors(
        input_count=input_count,
        input_width=input_width,
        random_vectors=random_vectors,
        seed=seed,
    )
    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width
    hex_digits = (input_bits + 3) // 4
    testbench = sanitize_identifier(module_name)

    lines = [
        f"module {testbench};",
        f"  localparam integer INPUT_BITS = {input_bits};",
        f"  localparam integer OUTPUT_BITS = {output_bits};",
        f"  localparam integer VECTOR_COUNT = {len(vectors)};",
        "",
        "  reg signed [INPUT_BITS-1:0] x_flat;",
        "  wire signed [OUTPUT_BITS-1:0] y_shared;",
        "  wire signed [OUTPUT_BITS-1:0] y_multiplier;",
        "  wire signed [OUTPUT_BITS-1:0] y_naive;",
        "  reg [INPUT_BITS-1:0] vectors [0:VECTOR_COUNT-1];",
        "  integer vector_index;",
        "",
        f"  {sanitize_identifier(shared_module)} shared (",
        "      .x_flat(x_flat),",
        "      .y_flat(y_shared)",
        "  );",
        f"  {sanitize_identifier(multiplier_module)} multiplier (",
        "      .x_flat(x_flat),",
        "      .y_flat(y_multiplier)",
        "  );",
        f"  {sanitize_identifier(naive_module)} naive (",
        "      .x_flat(x_flat),",
        "      .y_flat(y_naive)",
        "  );",
        "",
        "  task check_outputs;",
        "    begin",
        "      #1;",
        "      if ((y_shared !== y_multiplier) || (y_shared !== y_naive)) begin",
        '        $display("FAIL vector=%0d x=%h shared=%h multiplier=%h naive=%h",',
        "                 vector_index, x_flat, y_shared, y_multiplier, y_naive);",
        "        $fatal(1);",
        "      end",
        "    end",
        "  endtask",
        "",
        "  initial begin",
    ]
    for index, value in enumerate(vectors):
        lines.append(f"    vectors[{index}] = {input_bits}'h{value:0{hex_digits}x};")
    lines.extend(
        [
            "    for (vector_index = 0; vector_index < VECTOR_COUNT;",
            "         vector_index = vector_index + 1) begin",
            "      x_flat = vectors[vector_index];",
            "      check_outputs;",
            "    end",
            '    $display("PASS matched backends vectors=%0d", VECTOR_COUNT);',
            "    $finish;",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines), len(vectors)


def _tool_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-V"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout + completed.stderr).strip()
    return output.splitlines()[0] if output else "unknown"


def _run_simulation(
    *,
    output_dir: Path,
    shared_path: Path,
    multiplier_path: Path,
    naive_path: Path,
    testbench_path: Path,
    testbench_module: str,
) -> dict[str, Any]:
    iverilog = shutil.which("iverilog")
    vvp = shutil.which("vvp")
    if iverilog is None or vvp is None:
        raise BaselineError("--simulate requires both iverilog and vvp on PATH")

    executable = output_dir / "matched_backends.vvp"
    compile_result = subprocess.run(
        [
            iverilog,
            "-g2012",
            "-s",
            sanitize_identifier(testbench_module),
            "-o",
            str(executable),
            str(shared_path),
            str(multiplier_path),
            str(naive_path),
            str(testbench_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    (output_dir / "iverilog.stdout.txt").write_text(
        compile_result.stdout,
        encoding="utf-8",
    )
    (output_dir / "iverilog.stderr.txt").write_text(
        compile_result.stderr,
        encoding="utf-8",
    )
    if compile_result.returncode != 0:
        raise BaselineError("Icarus compilation failed; inspect iverilog.stderr.txt")

    simulation = subprocess.run(
        [vvp, str(executable)],
        check=False,
        capture_output=True,
        text=True,
    )
    (output_dir / "simulation.stdout.txt").write_text(
        simulation.stdout,
        encoding="utf-8",
    )
    (output_dir / "simulation.stderr.txt").write_text(
        simulation.stderr,
        encoding="utf-8",
    )
    if simulation.returncode != 0:
        raise BaselineError("matched-backend simulation failed; inspect simulation output")

    return {
        "performed": True,
        "passed": True,
        "iverilog_version": _tool_version(iverilog),
        "vvp_version": _tool_version(vvp),
        "stdout": "simulation.stdout.txt",
        "stderr": "simulation.stderr.txt",
    }


def build_matched_baselines(
    artifact_dir: Path,
    output_dir: Path,
    *,
    module_name: str | None = None,
    random_vectors: int = 256,
    seed: int = 0,
    simulate: bool = False,
) -> dict[str, Any]:
    """Build three matched RTL implementations from one compiler artifact."""

    source = artifact_dir.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    plan_path = source / "plan.json"
    codes_path = source / "codes.npy"
    manifest_path = source / "manifest.json"
    for required in (plan_path, codes_path, manifest_path):
        if not required.is_file():
            raise BaselineError(f"compiled artifact is missing {required.name}")

    plan = CompilationPlan.from_dict(_load_json(plan_path))
    codes = _load_codes(codes_path)
    if codes.shape != (plan.output_count, plan.input_count):
        raise BaselineError(
            f"codes shape {codes.shape} does not match plan "
            f"({plan.output_count}, {plan.input_count})"
        )

    source_manifest = _load_json(manifest_path)
    topology = source_manifest.get("topology", {})
    shared_module = str(topology.get("module", "")).strip()
    rtl_name = str(source_manifest.get("artifacts", {}).get("systemverilog", "")).strip()
    if not shared_module or not rtl_name:
        raise BaselineError("source manifest does not identify the generated RTL module")
    source_rtl = source / rtl_name
    if not source_rtl.is_file():
        raise BaselineError(f"source RTL does not exist: {source_rtl}")

    base = sanitize_identifier(module_name or f"{shared_module}_matched")
    multiplier_module = f"{base}_constant_multipliers"
    naive_module = f"{base}_naive_shift_add"
    testbench_module = f"{base}_tb"

    shared_path = output / "shared_dag.sv"
    multiplier_path = output / "constant_multipliers.sv"
    naive_path = output / "naive_shift_add.sv"
    testbench_path = output / "matched_testbench.sv"
    shutil.copyfile(source_rtl, shared_path)
    multiplier_path.write_text(
        emit_constant_multiplier_systemverilog(
            codes,
            input_width=plan.input_width,
            accumulator_width=plan.accumulator_width,
            module_name=multiplier_module,
        ),
        encoding="utf-8",
    )
    naive_path.write_text(
        emit_naive_shift_add_systemverilog(
            codes,
            input_width=plan.input_width,
            accumulator_width=plan.accumulator_width,
            module_name=naive_module,
        ),
        encoding="utf-8",
    )
    testbench, vector_count = emit_equivalence_testbench(
        shared_module=shared_module,
        multiplier_module=multiplier_module,
        naive_module=naive_module,
        input_count=plan.input_count,
        output_count=plan.output_count,
        input_width=plan.input_width,
        accumulator_width=plan.accumulator_width,
        random_vectors=random_vectors,
        seed=seed,
        module_name=testbench_module,
    )
    testbench_path.write_text(testbench, encoding="utf-8")

    nonzero = int(np.count_nonzero(codes))
    naive_adders = int(np.maximum(np.count_nonzero(codes, axis=1) - 1, 0).sum())
    simulation: dict[str, Any] = {"performed": False, "passed": False}
    if simulate:
        simulation = _run_simulation(
            output_dir=output,
            shared_path=shared_path,
            multiplier_path=multiplier_path,
            naive_path=naive_path,
            testbench_path=testbench_path,
            testbench_module=testbench_module,
        )

    artifacts = {
        "source_manifest": manifest_path,
        "source_plan": plan_path,
        "source_codes": codes_path,
        "shared_dag_rtl": shared_path,
        "constant_multiplier_rtl": multiplier_path,
        "naive_shift_add_rtl": naive_path,
        "testbench": testbench_path,
    }
    manifest = {
        "schema": "hephaestus.matched-baselines.v1",
        "source_artifact_dir": str(source),
        "contract": {
            "domain": "quantized_integer_core_before_row_scaling",
            "input_count": plan.input_count,
            "output_count": plan.output_count,
            "input_width": plan.input_width,
            "accumulator_width": plan.accumulator_width,
            "latency_cycles": 0,
            "combinational": True,
            "overflow_expected": False,
        },
        "backends": {
            "shared_dag": {
                "module": shared_module,
                "rtl": shared_path.name,
                "source_add_operators": plan.cse_add_count,
                "source_multiply_operators": 0,
                "cross_output_sharing": True,
                "runtime_coefficient_reads_per_matvec": 0,
            },
            "naive_shift_add": {
                "module": naive_module,
                "rtl": naive_path.name,
                "source_add_operators": naive_adders,
                "source_multiply_operators": 0,
                "cross_output_sharing": False,
                "runtime_coefficient_reads_per_matvec": 0,
            },
            "constant_multipliers": {
                "module": multiplier_module,
                "rtl": multiplier_path.name,
                "source_add_operators": naive_adders,
                "source_multiply_operators": nonzero,
                "cross_output_sharing": False,
                "runtime_coefficient_reads_per_matvec": 0,
            },
        },
        "verification": {
            "method": "self_checking_iverilog_comparison",
            "testbench": testbench_path.name,
            "seed": seed,
            "random_vectors_requested": random_vectors,
            "vectors_executed": vector_count,
            "simulation": simulation,
        },
        "artifact_sha256": {label: sha256_file(path) for label, path in artifacts.items()},
        "claims": {
            "matched_integer_contract_verified": bool(simulation.get("passed")),
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "matched_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate matched RTL baselines from one Hephaestus artifact."
    )
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path("build/matched-baselines"))
    parser.add_argument("--module")
    parser.add_argument("--verify-vectors", type=int, default=256)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--simulate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.verify_vectors < 0:
        print("error: --verify-vectors must be non-negative", file=sys.stderr)
        return 2
    try:
        manifest = build_matched_baselines(
            arguments.artifact_dir,
            arguments.out,
            module_name=arguments.module,
            random_vectors=arguments.verify_vectors,
            seed=arguments.seed,
            simulate=arguments.simulate,
        )
    except (BaselineError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    verification = manifest["verification"]
    print(
        f"generated three matched backends; vectors={verification['vectors_executed']} "
        f"simulation_passed={verification['simulation']['passed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
