"""Manifest and artifact helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .frontend import LoadedTensor
from .ir import Atom, CompilationPlan, NodeRef
from .quantize import QuantizedMatrix


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_values_sha256(loaded: LoadedTensor) -> str:
    """Hash exactly the numeric values consumed by the compiler, not a whole model shard."""

    canonical = np.ascontiguousarray(loaded.values, dtype="<f8")
    header = json.dumps(
        {"dtype": "float64-le", "shape": list(canonical.shape)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_bytes(header + b"\n" + canonical.tobytes(order="C"))


def _source_manifest(loaded: LoadedTensor) -> dict[str, Any]:
    source: dict[str, Any] = {
        "requested_path": str(loaded.requested_path),
        "descriptor_path": str(loaded.descriptor_path),
        "data_path": str(loaded.data_path),
        "data_file_size_bytes": loaded.data_path.stat().st_size,
        "tensor_key": loaded.tensor_key,
        "original_shape": list(loaded.original_shape),
        "selection": [{"start": start, "stop": stop} for start, stop in loaded.selection],
        "shape": list(loaded.selected_shape),
        "selected_values_sha256": selected_values_sha256(loaded),
    }
    if loaded.descriptor_path != loaded.data_path:
        source["descriptor_sha256"] = sha256_file(loaded.descriptor_path)
    return source


def topology_metrics(plan: CompilationPlan) -> dict[str, Any]:
    """Return structural metrics that remain meaningful before cell mapping."""

    fanout: dict[Atom | NodeRef, int] = {}

    def use(reference: Atom | NodeRef) -> None:
        fanout[reference] = fanout.get(reference, 0) + 1

    for node in plan.nodes:
        use(node.lhs)
        use(node.rhs)
    for output in plan.outputs:
        if output is not None:
            use(output)

    atom_fanouts = [count for reference, count in fanout.items() if isinstance(reference, Atom)]
    node_fanouts = [count for reference, count in fanout.items() if isinstance(reference, NodeRef)]
    savings = plan.naive_add_count - plan.cse_add_count
    return {
        "naive_add_count": plan.naive_add_count,
        "compiled_add_count": plan.cse_add_count,
        "shared_additions_saved": savings,
        "adder_savings_fraction": (savings / plan.naive_add_count if plan.naive_add_count else 0.0),
        "max_combinational_depth": plan.max_depth,
        "unique_atoms": len(atom_fanouts),
        "max_atom_fanout": max(atom_fanouts, default=0),
        "max_node_fanout": max(node_fanouts, default=0),
        "shared_node_count": sum(count > 1 for count in node_fanouts),
        "node_fanout_sum": sum(node_fanouts),
    }


def build_manifest(
    *,
    loaded: LoadedTensor,
    quantized: QuantizedMatrix,
    plan: CompilationPlan,
    module_name: str,
    rtl: str,
) -> dict[str, Any]:
    nonzero = int(np.count_nonzero(quantized.codes))
    total = int(quantized.codes.size)
    return {
        "schema": "hephaestus.manifest.v1",
        "compiler_version": __version__,
        "source": _source_manifest(loaded),
        "quantization": {
            "family": "signed-power-of-two",
            "target_bits": quantized.target_bits,
            "actual_index_bits": quantized.storage_bits,
            "max_shift": quantized.max_shift,
            "codebook": list(quantized.codebook),
            "row_scale_exponents": quantized.row_scale_exponents.tolist(),
            "weighted_mse": quantized.weighted_mse,
        },
        "topology": {
            "module": module_name,
            "input_width": plan.input_width,
            "accumulator_width": plan.accumulator_width,
            "nonzero_coefficients": nonzero,
            "coefficient_count": total,
            "density": nonzero / total,
            "runtime_weight_reads_per_matvec": 0,
            "runtime_weight_storage_bits_in_core": 0,
            **topology_metrics(plan),
        },
        "artifacts": {
            "systemverilog": f"{module_name}.sv",
            "systemverilog_sha256": sha256_bytes(rtl.encode("utf-8")),
            "plan": "plan.json",
            "codes": "codes.npy",
            "row_scale_exponents": "row_scale_exponents.npy",
        },
        "claims": {
            "bit_exact_integer_core_verified": False,
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
