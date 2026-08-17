"""Command-line interface for Hephaestus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .emit_sv import emit_systemverilog, sanitize_identifier
from .frontend import LoadedTensor, list_tensor_names, load_tensor
from .ir import CompilationPlan
from .lower import lower_codes
from .quantize import quantize_shift_add
from .reference import evaluate_codes, evaluate_plan
from .report import build_manifest, write_json


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _axis_slice(value: str) -> slice:
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("must use START:STOP, START:, or :STOP")

    def parse_endpoint(endpoint: str) -> int | None:
        if endpoint == "":
            return None
        parsed = int(endpoint)
        if parsed < 0:
            raise argparse.ArgumentTypeError("slice endpoints must be non-negative")
        return parsed

    start = parse_endpoint(parts[0])
    stop = parse_endpoint(parts[1])
    if start is not None and stop is not None and stop <= start:
        raise argparse.ArgumentTypeError("slice STOP must be greater than START")
    return slice(start, stop)


def _verify_random(
    plan: CompilationPlan,
    codes: np.ndarray,
    *,
    samples: int,
    seed: int,
) -> None:
    if samples <= 0:
        return
    rng = np.random.default_rng(seed)
    lower = -(1 << (plan.input_width - 1))
    upper = 1 << (plan.input_width - 1)
    for sample in range(samples):
        vector = rng.integers(lower, upper, size=plan.input_count, dtype=np.int64)
        expected = evaluate_codes(codes, vector)
        actual = evaluate_plan(plan, vector)
        if not np.array_equal(expected, actual):
            raise RuntimeError(
                f"bit-exact verification failed at sample {sample}: "
                f"expected {expected.tolist()}, got {actual.tolist()}"
            )


def _aligned_importance(
    source: str,
    *,
    tensor_key: str | None,
    weights: LoadedTensor,
) -> np.ndarray:
    importance = load_tensor(
        source,
        tensor_key=tensor_key,
        allowed_ndims=(1, 2),
    ).values
    selected_shape = weights.selected_shape
    original_shape = weights.original_shape

    if importance.ndim == 2 and importance.shape[0] == 1:
        importance = importance[0]

    if importance.ndim == 1:
        if importance.shape[0] == selected_shape[1]:
            return importance
        if importance.shape[0] == original_shape[1]:
            return importance[weights.selection_slices[1]]
        return importance

    if importance.shape == selected_shape:
        return importance
    if importance.shape == original_shape:
        return importance[weights.selection_slices]
    return importance


def compile_command(args: argparse.Namespace) -> int:
    source = Path(args.weights)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    module_name = sanitize_identifier(args.module or source.stem)

    loaded = load_tensor(
        source,
        tensor_key=args.tensor_key,
        axis_slices=(args.rows, args.columns),
        allowed_ndims=(2,),
    )
    weights = loaded.values
    importance = None
    if args.importance is not None:
        importance = _aligned_importance(
            args.importance,
            tensor_key=args.importance_tensor_key,
            weights=loaded,
        )

    quantized = quantize_shift_add(
        weights,
        target_bits=args.bits,
        max_shift=args.max_shift,
        importance=importance,
        exponent_search_radius=args.exponent_search_radius,
    )
    plan = lower_codes(
        quantized.codes,
        input_width=args.input_width,
        accumulator_width=args.accumulator_width,
        enable_cse=not args.no_cse,
        max_cse_nodes=args.max_cse_nodes,
    )
    rtl = emit_systemverilog(plan, module_name=module_name)

    _verify_random(plan, quantized.codes, samples=args.verify_samples, seed=args.seed)

    rtl_path = output_dir / f"{module_name}.sv"
    rtl_path.write_text(rtl, encoding="utf-8")
    np.save(output_dir / "codes.npy", quantized.codes, allow_pickle=False)
    np.save(
        output_dir / "row_scale_exponents.npy",
        quantized.row_scale_exponents,
        allow_pickle=False,
    )
    write_json(output_dir / "plan.json", plan.to_dict())
    manifest = build_manifest(
        loaded=loaded,
        quantized=quantized,
        plan=plan,
        module_name=module_name,
        rtl=rtl,
    )
    manifest["claims"]["bit_exact_integer_core_verified"] = args.verify_samples > 0
    manifest["verification"] = {
        "random_samples": args.verify_samples,
        "seed": args.seed,
    }
    write_json(output_dir / "manifest.json", manifest)

    print(f"compiled {weights.shape[0]}x{weights.shape[1]} matrix -> {rtl_path}")
    print(
        f"adders: {plan.cse_add_count} "
        f"(naive {plan.naive_add_count}, saved {plan.naive_add_count - plan.cse_add_count})"
    )
    print("runtime weight reads per matvec: 0")
    print(f"weighted quantization MSE: {quantized.weighted_mse:.8g}")
    return 0


def verify_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    plan = CompilationPlan.from_dict(
        json.loads((artifact_dir / "plan.json").read_text(encoding="utf-8"))
    )
    codes = np.load(artifact_dir / "codes.npy", allow_pickle=False)
    _verify_random(plan, codes, samples=args.samples, seed=args.seed)
    print(f"verified {args.samples} random vectors")
    return 0


def inspect_command(args: argparse.Namespace) -> int:
    manifest = json.loads((Path(args.artifact_dir) / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def tensors_command(args: argparse.Namespace) -> int:
    names = list_tensor_names(args.source)
    if args.json:
        print(json.dumps({"count": len(names), "tensors": list(names)}, indent=2))
    else:
        for name in names:
            print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hephaestus",
        description="Compile constant neural-network matrices into zero-weight-fetch RTL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile", help="quantize and lower one matrix")
    compile_parser.add_argument(
        "weights",
        help="JSON/NumPy/Safetensors file, index, or checkpoint directory",
    )
    compile_parser.add_argument("--tensor-key", help="tensor name for NPZ or Safetensors inputs")
    compile_parser.add_argument(
        "--rows",
        type=_axis_slice,
        help="materialize only output rows START:STOP",
    )
    compile_parser.add_argument(
        "--columns",
        type=_axis_slice,
        help="materialize only input columns START:STOP",
    )
    compile_parser.add_argument(
        "--importance",
        help="optional JSON/NumPy/Safetensors activation-importance tensor",
    )
    compile_parser.add_argument(
        "--importance-tensor-key",
        help="tensor name when --importance is NPZ or Safetensors",
    )
    compile_parser.add_argument("--out", default="build/hephaestus", help="artifact directory")
    compile_parser.add_argument("--module", help="generated SystemVerilog module name")
    compile_parser.add_argument(
        "--bits", type=_positive_int, default=3, help="codebook index budget"
    )
    compile_parser.add_argument("--max-shift", type=_nonnegative_int)
    compile_parser.add_argument("--input-width", type=_positive_int, default=8)
    compile_parser.add_argument("--accumulator-width", type=_positive_int)
    compile_parser.add_argument("--exponent-search-radius", type=_nonnegative_int, default=4)
    compile_parser.add_argument("--max-cse-nodes", type=_nonnegative_int, default=4096)
    compile_parser.add_argument("--no-cse", action="store_true")
    compile_parser.add_argument("--verify-samples", type=_nonnegative_int, default=64)
    compile_parser.add_argument("--seed", type=int, default=0)
    compile_parser.set_defaults(func=compile_command)

    verify_parser = subparsers.add_parser("verify", help="re-run bit-exact plan verification")
    verify_parser.add_argument("artifact_dir")
    verify_parser.add_argument("--samples", type=_positive_int, default=256)
    verify_parser.add_argument("--seed", type=int, default=1)
    verify_parser.set_defaults(func=verify_command)

    inspect_parser = subparsers.add_parser("inspect", help="print a build manifest")
    inspect_parser.add_argument("artifact_dir")
    inspect_parser.set_defaults(func=inspect_command)

    tensors_parser = subparsers.add_parser(
        "tensors",
        help="list tensor names without loading their payloads",
    )
    tensors_parser.add_argument("source")
    tensors_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    tensors_parser.set_defaults(func=tensors_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"hephaestus: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
