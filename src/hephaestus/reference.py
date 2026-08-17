"""Bit-exact Python reference evaluators for compiled plans."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .ir import Atom, CompilationPlan, ValueRef


ExactArray = NDArray[np.object_]


def evaluate_codes(codes: NDArray[np.integer], inputs: NDArray[np.integer]) -> ExactArray:
    matrix = np.asarray(codes, dtype=np.int64)
    vector = np.asarray(inputs, dtype=np.int64)
    if matrix.ndim != 2 or vector.ndim != 1 or vector.shape[0] != matrix.shape[1]:
        raise ValueError("shape mismatch between codes and inputs")
    return np.asarray(
        [
            sum(
                int(coefficient) * int(value)
                for coefficient, value in zip(row, vector, strict=True)
            )
            for row in matrix
        ],
        dtype=object,
    )


def evaluate_plan(plan: CompilationPlan, inputs: NDArray[np.integer]) -> ExactArray:
    vector = np.asarray(inputs, dtype=np.int64)
    if vector.ndim != 1 or vector.shape[0] != plan.input_count:
        raise ValueError("input vector has the wrong shape")

    values: dict[int, int] = {}

    def evaluate_ref(ref: ValueRef) -> int:
        if isinstance(ref, Atom):
            value = int(vector[ref.input_index]) << ref.shift
            return value if ref.sign > 0 else -value
        return values[ref.node_id]

    for node in plan.nodes:
        values[node.node_id] = evaluate_ref(node.lhs) + evaluate_ref(node.rhs)

    outputs: list[int] = []
    for ref in plan.outputs:
        outputs.append(0 if ref is None else evaluate_ref(ref))
    return np.asarray(outputs, dtype=object)
