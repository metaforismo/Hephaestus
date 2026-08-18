"""Generate matched registered streaming tiles from formally proved combinational cores."""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .lower import required_accumulator_width
from .report import sha256_file, write_json

IntArray = NDArray[np.int64]

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_ARTIFACT_LABELS = {
    "shared_dag": "shared_dag_rtl",
    "naive_shift_add": "naive_shift_add_rtl",
    "constant_multipliers": "constant_multiplier_rtl",
}
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BUBBLE_INTERVAL = 7


class RegisteredTileError(RuntimeError):
    """Raised when a registered matched-tile bundle cannot be produced safely."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegisteredTileError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegisteredTileError(f"JSON artifact must be an object: {path}")
    return value


def _load_codes(path: Path) -> IntArray:
    try:
        values = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise RegisteredTileError(f"cannot read quantized codes {path}: {exc}") from exc
    codes = np.asarray(values, dtype=np.int64)
    if codes.ndim != 2 or codes.size == 0:
        raise RegisteredTileError(f"codes must be a non-empty 2-D matrix, got {codes.shape}")
    return codes


def _safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise RegisteredTileError(f"{context} is not a safe module name: {value!r}")
    return value


def _safe_identifier(value: str, *, context: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_$]", "_", value)
    if not normalized or normalized[0].isdigit():
        normalized = f"h_{normalized}"
    return _safe_module(normalized, context=context)


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RegisteredTileError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _resolve_artifact(
    root: Path,
    raw_path: Any,
    expected_digest: Any,
    *,
    context: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise RegisteredTileError(f"{context}.path must be a non-empty string")
    digest = _require_digest(expected_digest, context=f"{context}.sha256")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise RegisteredTileError(f"{context}.path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise RegisteredTileError(f"{context}.path escapes the matched bundle") from exc
    if not resolved.is_file():
        raise RegisteredTileError(f"{context} does not exist: {resolved}")
    actual_digest = sha256_file(resolved)
    if actual_digest != digest:
        raise RegisteredTileError(
            f"{context} digest mismatch: expected {digest}, got {actual_digest}"
        )
    return resolved


def _positive_int(value: Any, *, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise RegisteredTileError(f"{context} must be a positive integer")
    return value


def _signed_literal(width: int, value: int) -> str:
    magnitude = abs(value)
    literal = f"{width}'sd{magnitude}"
    return literal if value >= 0 else f"-{literal}"


def _proof_passed(value: Any, *, negative: bool, context: str) -> None:
    if not isinstance(value, dict):
        raise RegisteredTileError(f"{context} proof is malformed")
    expected = {
        "performed": True,
        "passed": True,
        "proof_success": not negative,
        "counterexample_found": negative,
    }
    if any(value.get(key) is not expected_value for key, expected_value in expected.items()):
        outcome = "counterexample" if negative else "proof"
        raise RegisteredTileError(f"{context} did not preserve the required {outcome}")


def _validate_formal_evidence(
    path: Path,
    *,
    matched_manifest_path: Path,
    codes_path: Path,
    matched: dict[str, Any],
    input_bits: int,
    output_bits: int,
) -> dict[str, Any]:
    formal_path = path.resolve()
    formal = _load_json(formal_path)
    if formal.get("schema") != "hephaestus.formal-equivalence-evidence.v1":
        raise RegisteredTileError("unsupported formal-equivalence evidence schema")
    if formal.get("evidence_level") != "yosys_sat_combinational_equivalence":
        raise RegisteredTileError("unsupported formal-equivalence evidence level")

    claims = formal.get("claims")
    required_claims = (
        "matched_integer_contract_verified",
        "exhaustive_combinational_equivalence_verified",
        "negative_control_counterexample_found",
    )
    if not isinstance(claims, dict) or any(
        claims.get(name) is not True for name in required_claims
    ):
        raise RegisteredTileError("source formal evidence is not fully verified")
    if claims.get("sequential_equivalence_verified") is not False:
        raise RegisteredTileError("source formal evidence has an invalid sequential claim")

    source = formal.get("source")
    if not isinstance(source, dict):
        raise RegisteredTileError("source formal-evidence binding is malformed")
    if source.get("matched_manifest_sha256") != sha256_file(matched_manifest_path):
        raise RegisteredTileError("formal evidence is bound to a different matched manifest")
    if source.get("codes_sha256") != sha256_file(codes_path):
        raise RegisteredTileError("formal evidence is bound to different quantized codes")

    scope = formal.get("scope")
    if not isinstance(scope, dict):
        raise RegisteredTileError("source formal-evidence scope is malformed")
    if (
        scope.get("input_bits") != input_bits
        or scope.get("output_bits") != output_bits
        or scope.get("combinational") is not True
        or scope.get("sequential_depth") != 0
    ):
        raise RegisteredTileError("source formal-evidence scope differs from the matched contract")

    matched_backends = matched.get("backends")
    formal_backends = formal.get("backends")
    if not isinstance(matched_backends, dict) or not isinstance(formal_backends, dict):
        raise RegisteredTileError("source backend evidence is malformed")
    if set(formal_backends) != set(_BACKENDS):
        raise RegisteredTileError("formal evidence does not cover the three matched backends")
    for backend_name in _BACKENDS:
        matched_backend = matched_backends[backend_name]
        formal_backend = formal_backends[backend_name]
        if not isinstance(matched_backend, dict) or not isinstance(formal_backend, dict):
            raise RegisteredTileError(f"formal backend {backend_name!r} is malformed")
        if formal_backend.get("module") != matched_backend.get("module"):
            raise RegisteredTileError(f"formal backend module differs for {backend_name}")
        if formal_backend.get("source_rtl") != matched_backend.get("rtl"):
            raise RegisteredTileError(f"formal backend RTL differs for {backend_name}")
        if formal_backend.get("exhaustive_over_defined_inputs") is not True:
            raise RegisteredTileError(f"formal backend {backend_name} is not exhaustive")
        _proof_passed(
            formal_backend.get("proof"),
            negative=False,
            context=f"formal backend {backend_name}",
        )

    negative_control = formal.get("negative_control")
    if not isinstance(negative_control, dict):
        raise RegisteredTileError("formal negative control is malformed")
    _proof_passed(
        negative_control.get("proof"),
        negative=True,
        context="formal negative control",
    )
    return formal


def emit_reference_core(
    codes: NDArray[np.integer],
    *,
    input_width: int,
    accumulator_width: int,
    module_name: str,
) -> str:
    """Emit an independent combinational reference directly from the code matrix."""

    matrix = np.asarray(codes, dtype=np.int64)
    if matrix.ndim != 2 or matrix.size == 0:
        raise ValueError(f"codes must be a non-empty 2-D matrix, got {matrix.shape}")
    minimum_width = required_accumulator_width(matrix, input_width)
    if accumulator_width < minimum_width:
        raise ValueError(
            f"accumulator_width={accumulator_width} is unsafe; "
            f"at least {minimum_width} bits are required"
        )
    module = _safe_module(module_name, context="reference module")
    rows, columns = matrix.shape
    input_bits = columns * input_width
    output_bits = rows * accumulator_width
    extension = accumulator_width - input_width
    lines = [
        "// Independent arithmetic reference generated directly from source_codes.npy.",
        f"module {module} (",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        f"    output wire signed [{output_bits - 1}:0] y_flat",
        ");",
        f"  localparam integer INPUT_WIDTH = {input_width};",
        f"  localparam integer ACC_WIDTH = {accumulator_width};",
        "",
    ]
    for column_index in range(columns):
        lines.extend(
            [
                f"  wire signed [{input_width - 1}:0] x_{column_index};",
                f"  wire signed [{accumulator_width - 1}:0] sx_{column_index};",
                (
                    f"  assign x_{column_index} = "
                    f"x_flat[{column_index * input_width} +: INPUT_WIDTH];"
                ),
            ]
        )
        if extension == 0:
            lines.append(f"  assign sx_{column_index} = x_{column_index};")
        else:
            lines.append(
                f"  assign sx_{column_index} = "
                f"{{{{{extension}{{x_{column_index}[INPUT_WIDTH-1]}}}}, x_{column_index}}};"
            )
    lines.append("")
    for row_index, row in enumerate(matrix):
        terms: list[str] = []
        for column_index, raw_coefficient in enumerate(row):
            coefficient = int(raw_coefficient)
            if coefficient == 0:
                continue
            term = f"product_o{row_index}_i{column_index}"
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
        accumulator = f"reference_o{row_index}"
        lines.append(f"  reg signed [{accumulator_width - 1}:0] {accumulator};")
        lines.append("  always @* begin")
        lines.append(f"    {accumulator} = {{ACC_WIDTH{{1'b0}}}};")
        for term in terms:
            lines.append(f"    {accumulator} = {accumulator} + {term};")
        lines.append("  end")
        lines.append(
            f"  assign y_flat[{row_index * accumulator_width} +: ACC_WIDTH] = {accumulator};"
        )
        lines.append("")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def emit_registered_wrapper(
    *,
    core_module: str,
    module_name: str,
    input_bits: int,
    output_bits: int,
    inject_fault: bool = False,
) -> str:
    """Wrap a zero-cycle core in matched input/output registers and a valid pipeline."""

    if input_bits <= 0 or output_bits <= 0:
        raise ValueError("registered wrapper bus widths must be positive")
    core = _safe_module(core_module, context="core module")
    module = _safe_module(module_name, context="wrapper module")
    lines = [
        "// Matched registered streaming boundary for a combinational integer core.",
        "// A value sampled with valid_in at edge N appears with valid_out at edge N+1.",
        f"module {module} (",
        "    input  wire clk,",
        "    input  wire reset,",
        "    input  wire valid_in,",
        f"    input  wire signed [{input_bits - 1}:0] x_flat,",
        "    output reg valid_out,",
        f"    output reg signed [{output_bits - 1}:0] y_flat",
        ");",
        f"  reg signed [{input_bits - 1}:0] x_q;",
        "  reg valid_q;",
        f"  wire signed [{output_bits - 1}:0] y_comb;",
        f"  {core} core (",
        "      .x_flat(x_q),",
        "      .y_flat(y_comb)",
        "  );",
    ]
    captured = "y_comb"
    if inject_fault:
        lines.extend(
            [
                f"  wire [{output_bits - 1}:0] fault_mask;",
                f"  wire signed [{output_bits - 1}:0] y_faulted;",
                (f"  assign fault_mask = {{{{{output_bits - 1}{{1'b0}}}}, (x_q[0] & valid_q)}};"),
                "  assign y_faulted = y_comb ^ fault_mask;",
            ]
        )
        captured = "y_faulted"
    lines.extend(
        [
            "  always @(posedge clk) begin",
            "    if (reset) begin",
            f"      x_q <= {input_bits}'sd0;",
            "      valid_q <= 1'b0;",
            "      valid_out <= 1'b0;",
            f"      y_flat <= {output_bits}'sd0;",
            "    end else begin",
            "      x_q <= x_flat;",
            "      valid_q <= valid_in;",
            "      valid_out <= valid_q;",
            f"      y_flat <= {captured};",
            "    end",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def _pack_lanes(values: list[int], width: int) -> int:
    mask = (1 << width) - 1
    packed = 0
    for index, value in enumerate(values):
        packed |= (value & mask) << (index * width)
    return packed


def _unpack_signed_lanes(packed: int, *, count: int, width: int) -> list[int]:
    mask = (1 << width) - 1
    sign = 1 << (width - 1)
    values: list[int] = []
    for index in range(count):
        raw = (packed >> (index * width)) & mask
        values.append(raw - (1 << width) if raw & sign else raw)
    return values


def _evaluate_packed(
    codes: IntArray,
    packed_input: int,
    *,
    input_width: int,
    accumulator_width: int,
) -> int:
    lanes = _unpack_signed_lanes(
        packed_input,
        count=int(codes.shape[1]),
        width=input_width,
    )
    minimum = -(1 << (accumulator_width - 1))
    maximum = (1 << (accumulator_width - 1)) - 1
    outputs: list[int] = []
    for row in codes:
        value = sum(int(coefficient) * lane for coefficient, lane in zip(row, lanes, strict=True))
        if value < minimum or value > maximum:
            raise RegisteredTileError(
                f"oracle output {value} does not fit signed {accumulator_width}-bit lane"
            )
        outputs.append(value)
    return _pack_lanes(outputs, accumulator_width)


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
    input_bits = input_count * input_width
    vectors.extend(rng.getrandbits(input_bits) for _ in range(random_vectors))
    return list(dict.fromkeys(vectors))


def _verification_schedule(
    vectors: list[int],
    outputs: list[int],
) -> list[dict[str, int | bool]]:
    if len(vectors) != len(outputs):
        raise ValueError("verification inputs and outputs must have equal length")
    schedule: list[dict[str, int | bool]] = []
    for index, (packed_input, packed_output) in enumerate(zip(vectors, outputs, strict=True)):
        schedule.append(
            {
                "valid": True,
                "input": packed_input,
                "output": packed_output,
            }
        )
        if index % _BUBBLE_INTERVAL == _BUBBLE_INTERVAL // 2:
            schedule.append({"valid": False, "input": 0, "output": 0})
    return schedule


def emit_registered_testbench(
    *,
    backend_modules: dict[str, str],
    schedule: list[dict[str, int | bool]],
    input_bits: int,
    output_bits: int,
    valid_vector_count: int,
    module_name: str,
) -> str:
    """Emit an oracle-driven streaming testbench with continuous traffic and bubbles."""

    if set(backend_modules) != set(_BACKENDS):
        raise ValueError("registered testbench requires the three matched backends")
    if input_bits <= 0 or output_bits <= 0 or valid_vector_count <= 0:
        raise ValueError("registered testbench dimensions must be positive")
    if not schedule:
        raise ValueError("registered testbench schedule must not be empty")
    modules = {
        name: _safe_module(module, context=f"{name} wrapper")
        for name, module in backend_modules.items()
    }
    module = _safe_module(module_name, context="testbench module")
    input_hex_digits = (input_bits + 3) // 4
    output_hex_digits = (output_bits + 3) // 4
    lines = [
        f"module {module};",
        f"  localparam integer INPUT_BITS = {input_bits};",
        f"  localparam integer OUTPUT_BITS = {output_bits};",
        f"  localparam integer SCHEDULE_COUNT = {len(schedule)};",
        f"  localparam integer VALID_VECTOR_COUNT = {valid_vector_count};",
        "  reg clk;",
        "  reg reset;",
        "  reg valid_in;",
        "  reg signed [INPUT_BITS-1:0] x_flat;",
        "  reg schedule_valid [0:SCHEDULE_COUNT-1];",
        "  reg [INPUT_BITS-1:0] schedule_input [0:SCHEDULE_COUNT-1];",
        "  reg [OUTPUT_BITS-1:0] schedule_output [0:SCHEDULE_COUNT-1];",
        "  reg expected_valid;",
        "  reg [OUTPUT_BITS-1:0] expected_y;",
        "  integer schedule_index;",
        "  integer checked_vectors;",
        "",
    ]
    for backend in _BACKENDS:
        lines.extend(
            [
                f"  wire valid_{backend};",
                f"  wire signed [OUTPUT_BITS-1:0] y_{backend};",
            ]
        )
    lines.extend(["", "  always #5 clk = ~clk;", ""])
    for backend in _BACKENDS:
        lines.extend(
            [
                f"  {modules[backend]} {backend} (",
                "      .clk(clk),",
                "      .reset(reset),",
                "      .valid_in(valid_in),",
                "      .x_flat(x_flat),",
                f"      .valid_out(valid_{backend}),",
                f"      .y_flat(y_{backend})",
                "  );",
            ]
        )
    lines.extend(
        [
            "  task check_outputs;",
            "    begin",
            "      #1;",
            "      if (reset) begin",
            (
                "        if (valid_shared_dag || valid_naive_shift_add || "
                "valid_constant_multipliers) begin"
            ),
            '          $display("FAIL reset did not clear valid pipeline");',
            "          $fatal(1);",
            "        end",
            (
                "        if ((y_shared_dag !== {OUTPUT_BITS{1'b0}}) || "
                "(y_naive_shift_add !== {OUTPUT_BITS{1'b0}}) || "
                "(y_constant_multipliers !== {OUTPUT_BITS{1'b0}})) begin"
            ),
            '          $display("FAIL reset did not clear output pipeline");',
            "          $fatal(1);",
            "        end",
            "      end",
            (
                "      if ((valid_shared_dag !== expected_valid) || "
                "(valid_naive_shift_add !== expected_valid) || "
                "(valid_constant_multipliers !== expected_valid)) begin"
            ),
            '        $display("FAIL valid alignment expected=%b shared=%b naive=%b mult=%b",',
            "                 expected_valid, valid_shared_dag, valid_naive_shift_add,",
            "                 valid_constant_multipliers);",
            "        $fatal(1);",
            "      end",
            "      if (expected_valid) begin",
            (
                "        if ((y_shared_dag !== expected_y) || "
                "(y_naive_shift_add !== expected_y) || "
                "(y_constant_multipliers !== expected_y)) begin"
            ),
            '          $display("FAIL expected=%h shared=%h naive=%h mult=%h",',
            "                   expected_y, y_shared_dag, y_naive_shift_add,",
            "                   y_constant_multipliers);",
            "          $fatal(1);",
            "        end",
            "        checked_vectors = checked_vectors + 1;",
            "      end",
            "    end",
            "  endtask",
            "",
            "  initial begin",
            "    clk = 1'b0;",
            "    reset = 1'b1;",
            "    valid_in = 1'b0;",
            "    x_flat = {INPUT_BITS{1'b0}};",
            "    expected_valid = 1'b0;",
            "    expected_y = {OUTPUT_BITS{1'b0}};",
            "    checked_vectors = 0;",
        ]
    )
    for index, item in enumerate(schedule):
        valid = "1'b1" if item["valid"] else "1'b0"
        packed_input = int(item["input"])
        packed_output = int(item["output"])
        lines.extend(
            [
                f"    schedule_valid[{index}] = {valid};",
                (
                    f"    schedule_input[{index}] = "
                    f"{input_bits}'h{packed_input:0{input_hex_digits}x};"
                ),
                (
                    f"    schedule_output[{index}] = "
                    f"{output_bits}'h{packed_output:0{output_hex_digits}x};"
                ),
            ]
        )
    lines.extend(
        [
            "    repeat (2) begin",
            "      @(posedge clk);",
            "      check_outputs;",
            "    end",
            "    @(negedge clk);",
            "    reset = 1'b0;",
            "    for (schedule_index = 0; schedule_index < SCHEDULE_COUNT;",
            "         schedule_index = schedule_index + 1) begin",
            "      valid_in = schedule_valid[schedule_index];",
            "      x_flat = schedule_input[schedule_index];",
            "      @(posedge clk);",
            "      check_outputs;",
            "      expected_valid = schedule_valid[schedule_index];",
            "      expected_y = schedule_output[schedule_index];",
            "      @(negedge clk);",
            "    end",
            "    valid_in = 1'b0;",
            "    x_flat = {INPUT_BITS{1'b0}};",
            "    @(posedge clk);",
            "    check_outputs;",
            "    expected_valid = 1'b0;",
            "    expected_y = {OUTPUT_BITS{1'b0}};",
            "    @(negedge clk);",
            "    @(posedge clk);",
            "    check_outputs;",
            "    if (checked_vectors != VALID_VECTOR_COUNT) begin",
            (
                '      $display("FAIL checked=%0d expected=%0d", '
                "checked_vectors, VALID_VECTOR_COUNT);"
            ),
            "      $fatal(1);",
            "    end",
            '    $display("PASS registered matched tiles vectors=%0d", checked_vectors);',
            "    $finish;",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def _tool_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-V"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout + completed.stderr).strip()
    return output.splitlines()[0] if output else "unknown"


def _run_simulation(
    *,
    output_dir: Path,
    sources: list[Path],
    testbench_module: str,
    label: str,
    expect_failure: bool,
) -> dict[str, Any]:
    iverilog = shutil.which("iverilog")
    vvp = shutil.which("vvp")
    if iverilog is None or vvp is None:
        raise RegisteredTileError("--simulate requires both iverilog and vvp on PATH")
    executable = output_dir / f"{label}.vvp"
    compile_result = subprocess.run(
        [
            iverilog,
            "-g2012",
            "-s",
            testbench_module,
            "-o",
            str(executable),
            *[str(path) for path in sources],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    compile_stdout = output_dir / f"{label}.iverilog.stdout.txt"
    compile_stderr = output_dir / f"{label}.iverilog.stderr.txt"
    compile_stdout.write_text(compile_result.stdout, encoding="utf-8")
    compile_stderr.write_text(compile_result.stderr, encoding="utf-8")
    if compile_result.returncode != 0:
        raise RegisteredTileError(
            f"Icarus compilation failed for {label}; inspect {compile_stderr.name}"
        )
    simulation = subprocess.run(
        [vvp, str(executable)],
        check=False,
        capture_output=True,
        text=True,
    )
    simulation_stdout = output_dir / f"{label}.simulation.stdout.txt"
    simulation_stderr = output_dir / f"{label}.simulation.stderr.txt"
    simulation_stdout.write_text(simulation.stdout, encoding="utf-8")
    simulation_stderr.write_text(simulation.stderr, encoding="utf-8")
    positive_marker = "PASS registered matched tiles" in simulation.stdout
    negative_marker = "FAIL expected=" in simulation.stdout
    if expect_failure:
        passed = simulation.returncode != 0 and negative_marker and not positive_marker
    else:
        passed = simulation.returncode == 0 and positive_marker and not negative_marker
    if not passed:
        expectation = "fault detection" if expect_failure else "a passing comparison"
        raise RegisteredTileError(
            f"registered-tile simulation did not produce {expectation} for {label}"
        )
    return {
        "performed": True,
        "passed": True,
        "expected_failure": expect_failure,
        "returncode": simulation.returncode,
        "iverilog_version": _tool_version(iverilog),
        "vvp_version": _tool_version(vvp),
        "compile_stdout": compile_stdout.name,
        "compile_stderr": compile_stderr.name,
        "simulation_stdout": simulation_stdout.name,
        "simulation_stderr": simulation_stderr.name,
    }


def build_registered_tiles(
    matched_bundle: Path,
    codes_path: Path,
    formal_evidence_path: Path,
    output_dir: Path,
    *,
    module_name: str = "hephaestus_registered",
    random_vectors: int = 256,
    seed: int = 0,
    simulate: bool = False,
) -> dict[str, Any]:
    """Build identical registered boundaries around three exhaustively proved cores."""

    if random_vectors < 0:
        raise ValueError("random_vectors must be non-negative")
    bundle = matched_bundle.resolve()
    source_codes = codes_path.resolve()
    source_formal = formal_evidence_path.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    manifest_path = bundle / "matched_manifest.json"
    if not manifest_path.is_file():
        raise RegisteredTileError("matched bundle is missing matched_manifest.json")
    matched = _load_json(manifest_path)
    if matched.get("schema") != "hephaestus.matched-baselines.v1":
        raise RegisteredTileError("unsupported matched-baseline manifest schema")
    claims = matched.get("claims")
    if not isinstance(claims, dict) or claims.get("matched_integer_contract_verified") is not True:
        raise RegisteredTileError("matched integer contract must be verified before registration")

    contract = matched.get("contract")
    if not isinstance(contract, dict):
        raise RegisteredTileError("matched manifest contract is malformed")
    if contract.get("combinational") is not True or contract.get("latency_cycles") != 0:
        raise RegisteredTileError("registered tiles require zero-cycle combinational source cores")
    input_count = _positive_int(contract.get("input_count"), context="input_count")
    output_count = _positive_int(contract.get("output_count"), context="output_count")
    input_width = _positive_int(contract.get("input_width"), context="input_width")
    accumulator_width = _positive_int(
        contract.get("accumulator_width"), context="accumulator_width"
    )
    input_bits = input_count * input_width
    output_bits = output_count * accumulator_width

    backends = matched.get("backends")
    artifact_sha256 = matched.get("artifact_sha256")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise RegisteredTileError("matched manifest does not contain the three expected backends")
    if not isinstance(artifact_sha256, dict):
        raise RegisteredTileError("matched manifest artifact digests are malformed")

    expected_codes_digest = _require_digest(
        artifact_sha256.get("source_codes"),
        context="matched source codes digest",
    )
    if not source_codes.is_file():
        raise RegisteredTileError(f"quantized codes do not exist: {source_codes}")
    if sha256_file(source_codes) != expected_codes_digest:
        raise RegisteredTileError("source codes do not match the matched-baseline manifest")
    codes = _load_codes(source_codes)
    if codes.shape != (output_count, input_count):
        raise RegisteredTileError(
            f"codes shape {codes.shape} does not match contract ({output_count}, {input_count})"
        )
    minimum_width = required_accumulator_width(codes, input_width)
    if accumulator_width < minimum_width:
        raise RegisteredTileError(
            f"contract accumulator width {accumulator_width} is smaller than {minimum_width}"
        )

    _validate_formal_evidence(
        source_formal,
        matched_manifest_path=manifest_path,
        codes_path=source_codes,
        matched=matched,
        input_bits=input_bits,
        output_bits=output_bits,
    )

    base = _safe_identifier(module_name, context="registered module base")
    preserved_manifest = output / "source_matched_manifest.json"
    preserved_codes = output / "source_codes.npy"
    preserved_formal = output / "source_formal_evidence.json"
    shutil.copyfile(manifest_path, preserved_manifest)
    shutil.copyfile(source_codes, preserved_codes)
    shutil.copyfile(source_formal, preserved_formal)

    backend_manifest: dict[str, dict[str, Any]] = {}
    simulation_sources: list[Path] = []
    artifacts: dict[str, Path] = {
        "source_matched_manifest": preserved_manifest,
        "source_codes": preserved_codes,
        "source_formal_evidence": preserved_formal,
    }
    for backend_name in _BACKENDS:
        backend = backends[backend_name]
        if not isinstance(backend, dict):
            raise RegisteredTileError(f"backend {backend_name!r} is malformed")
        if backend.get("runtime_coefficient_reads_per_matvec") != 0:
            raise RegisteredTileError(f"backend {backend_name} is not a zero-fetch core")
        core_module = _safe_module(backend.get("module"), context=f"{backend_name}.module")
        digest_label = _ARTIFACT_LABELS[backend_name]
        source_rtl = _resolve_artifact(
            bundle,
            backend.get("rtl"),
            artifact_sha256.get(digest_label),
            context=f"{backend_name}.rtl",
        )
        core_path = output / f"{backend_name}_core.sv"
        wrapper_path = output / f"{backend_name}_registered.sv"
        shutil.copyfile(source_rtl, core_path)
        wrapper_module = f"{base}_{backend_name}_registered"
        wrapper_path.write_text(
            emit_registered_wrapper(
                core_module=core_module,
                module_name=wrapper_module,
                input_bits=input_bits,
                output_bits=output_bits,
            ),
            encoding="utf-8",
        )
        simulation_sources.extend([core_path, wrapper_path])
        artifacts[f"{backend_name}_core"] = core_path
        artifacts[f"{backend_name}_wrapper"] = wrapper_path
        backend_manifest[backend_name] = {
            "core_module": core_module,
            "wrapper_module": wrapper_module,
            "core_rtl": core_path.name,
            "wrapper_rtl": wrapper_path.name,
            "core_sha256": sha256_file(core_path),
            "wrapper_sha256": sha256_file(wrapper_path),
            "runtime_coefficient_reads_per_matvec": 0,
        }

    reference_core_module = f"{base}_reference_core"
    reference_core_path = output / "reference_core.sv"
    reference_core_path.write_text(
        emit_reference_core(
            codes,
            input_width=input_width,
            accumulator_width=accumulator_width,
            module_name=reference_core_module,
        ),
        encoding="utf-8",
    )
    artifacts["reference_core"] = reference_core_path

    vectors = _verification_vectors(
        input_count=input_count,
        input_width=input_width,
        random_vectors=random_vectors,
        seed=seed,
    )
    outputs = [
        _evaluate_packed(
            codes,
            packed,
            input_width=input_width,
            accumulator_width=accumulator_width,
        )
        for packed in vectors
    ]
    schedule = _verification_schedule(vectors, outputs)
    oracle_path = output / "verification_oracle.json"
    input_hex_digits = (input_bits + 3) // 4
    output_hex_digits = (output_bits + 3) // 4
    write_json(
        oracle_path,
        {
            "schema": "hephaestus.registered-tile-oracle.v1",
            "input_bits": input_bits,
            "output_bits": output_bits,
            "seed": seed,
            "random_vectors_requested": random_vectors,
            "bubble_interval": _BUBBLE_INTERVAL,
            "vectors": [
                {
                    "input_hex": f"{packed_input:0{input_hex_digits}x}",
                    "output_hex": f"{packed_output:0{output_hex_digits}x}",
                }
                for packed_input, packed_output in zip(vectors, outputs, strict=True)
            ],
            "schedule": [
                {
                    "valid": bool(item["valid"]),
                    "input_hex": f"{int(item['input']):0{input_hex_digits}x}",
                    "output_hex": f"{int(item['output']):0{output_hex_digits}x}",
                }
                for item in schedule
            ],
        },
    )
    artifacts["verification_oracle"] = oracle_path

    positive_tb_module = f"{base}_registered_tb"
    positive_tb_path = output / "registered_testbench.sv"
    positive_tb_path.write_text(
        emit_registered_testbench(
            backend_modules={
                backend: backend_manifest[backend]["wrapper_module"] for backend in _BACKENDS
            },
            schedule=schedule,
            input_bits=input_bits,
            output_bits=output_bits,
            valid_vector_count=len(vectors),
            module_name=positive_tb_module,
        ),
        encoding="utf-8",
    )
    artifacts["testbench"] = positive_tb_path

    negative_wrapper_module = f"{base}_negative_control_registered"
    negative_wrapper_path = output / "negative_control_registered.sv"
    negative_wrapper_path.write_text(
        emit_registered_wrapper(
            core_module=backend_manifest["shared_dag"]["core_module"],
            module_name=negative_wrapper_module,
            input_bits=input_bits,
            output_bits=output_bits,
            inject_fault=True,
        ),
        encoding="utf-8",
    )
    negative_tb_module = f"{base}_negative_control_tb"
    negative_tb_path = output / "negative_control_testbench.sv"
    negative_tb_path.write_text(
        emit_registered_testbench(
            backend_modules={
                "shared_dag": negative_wrapper_module,
                "naive_shift_add": backend_manifest["naive_shift_add"]["wrapper_module"],
                "constant_multipliers": backend_manifest["constant_multipliers"]["wrapper_module"],
            },
            schedule=schedule,
            input_bits=input_bits,
            output_bits=output_bits,
            valid_vector_count=len(vectors),
            module_name=negative_tb_module,
        ),
        encoding="utf-8",
    )
    artifacts["negative_control_wrapper"] = negative_wrapper_path
    artifacts["negative_control_testbench"] = negative_tb_path

    simulation: dict[str, Any] = {
        "performed": False,
        "passed": False,
        "negative_control_performed": False,
        "negative_control_detected": False,
    }
    if simulate:
        positive = _run_simulation(
            output_dir=output,
            sources=[*simulation_sources, positive_tb_path],
            testbench_module=positive_tb_module,
            label="positive",
            expect_failure=False,
        )
        negative = _run_simulation(
            output_dir=output,
            sources=[
                *simulation_sources,
                negative_wrapper_path,
                negative_tb_path,
            ],
            testbench_module=negative_tb_module,
            label="negative_control",
            expect_failure=True,
        )
        simulation = {
            "performed": True,
            "passed": True,
            "positive": positive,
            "negative_control_performed": True,
            "negative_control_detected": True,
            "negative_control": negative,
        }
        for path in output.glob("positive.*.txt"):
            artifacts[path.name] = path
        for path in output.glob("negative_control.*.txt"):
            artifacts[path.name] = path

    manifest = {
        "schema": "hephaestus.registered-matched-tiles.v1",
        "evidence_level": "registered_streaming_tiles_bound_to_formally_proved_cores",
        "source": {
            "matched_manifest": preserved_manifest.name,
            "matched_manifest_sha256": sha256_file(preserved_manifest),
            "codes": preserved_codes.name,
            "codes_sha256": sha256_file(preserved_codes),
            "formal_evidence": preserved_formal.name,
            "formal_evidence_sha256": sha256_file(preserved_formal),
        },
        "contract": {
            "domain": contract.get("domain"),
            "input_count": input_count,
            "output_count": output_count,
            "input_width": input_width,
            "accumulator_width": accumulator_width,
            "input_bits": input_bits,
            "output_bits": output_bits,
            "latency_cycles": 1,
            "valid_latency_cycles": 1,
            "initiation_interval_cycles": 1,
            "clock_edge": "rising",
            "reset_style": "synchronous_active_high",
            "reset_clears_pipeline": True,
            "input_registered": True,
            "output_registered": True,
            "combinational_core_preserved": True,
            "runtime_coefficient_reads_per_matvec": 0,
        },
        "backends": backend_manifest,
        "reference": {
            "module": reference_core_module,
            "rtl": reference_core_path.name,
            "sha256": sha256_file(reference_core_path),
            "derived_directly_from_codes": True,
            "uses_compilation_plan": False,
        },
        "verification": {
            "method": "self_checking_oracle_driven_registered_streaming_simulation",
            "testbench": positive_tb_path.name,
            "negative_control_testbench": negative_tb_path.name,
            "oracle": oracle_path.name,
            "seed": seed,
            "random_vectors_requested": random_vectors,
            "valid_vectors_executed": len(vectors),
            "schedule_cycles": len(schedule),
            "bubble_cycles": len(schedule) - len(vectors),
            "simulation": simulation,
        },
        "artifact_sha256": {label: sha256_file(path) for label, path in artifacts.items()},
        "claims": {
            "source_matched_integer_contract_verified": True,
            "source_exhaustive_combinational_equivalence_verified": True,
            "source_formal_negative_control_counterexample_found": True,
            "registered_streaming_interface_generated": True,
            "registered_backends_match_oracle_on_executed_schedule": bool(simulation.get("passed")),
            "one_cycle_latency_verified_on_executed_schedule": bool(simulation.get("passed")),
            "initiation_interval_one_verified_on_executed_schedule": bool(simulation.get("passed")),
            "reset_flush_verified_on_executed_schedule": bool(simulation.get("passed")),
            "simulation_negative_control_detected": bool(
                simulation.get("negative_control_detected")
            ),
            "sequential_formal_equivalence_verified": False,
            "post_synthesis_ppa_measured": False,
            "placement_performed": False,
            "routing_performed": False,
            "power_estimated": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "registered_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate matched registered streaming tiles from formally proved cores."
    )
    parser.add_argument("matched_bundle", type=Path)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--formal-evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("build/registered-tiles"))
    parser.add_argument("--module", default="hephaestus_registered")
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
        manifest = build_registered_tiles(
            arguments.matched_bundle,
            arguments.codes,
            arguments.formal_evidence,
            arguments.out,
            module_name=arguments.module,
            random_vectors=arguments.verify_vectors,
            seed=arguments.seed,
            simulate=arguments.simulate,
        )
    except (RegisteredTileError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        "generated three registered matched tiles; "
        f"valid_vectors={manifest['verification']['valid_vectors_executed']} "
        f"simulation_passed={manifest['verification']['simulation']['passed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
