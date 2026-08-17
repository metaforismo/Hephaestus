"""Lower power-of-two coefficient matrices into a shared addition DAG."""

from __future__ import annotations

from collections import Counter
from math import ceil, log2

import numpy as np
from numpy.typing import NDArray

from .ir import AddNode, Atom, CompilationPlan, NodeRef, ValueRef, ref_sort_key

IntArray = NDArray[np.int64]


def _coefficient_to_atom(input_index: int, coefficient: int) -> Atom:
    if coefficient == 0:
        raise ValueError("zero has no atom")
    magnitude = abs(coefficient)
    if magnitude & (magnitude - 1):
        raise ValueError(f"coefficient {coefficient} is not a signed power of two")
    return Atom(
        input_index=input_index,
        shift=magnitude.bit_length() - 1,
        sign=1 if coefficient > 0 else -1,
    )


def required_accumulator_width(codes: IntArray, input_width: int) -> int:
    """Compute a conservative exact signed width for the integer dot products."""

    if input_width < 2:
        raise ValueError("input_width must be at least 2")
    max_input_magnitude = 1 << (input_width - 1)
    maximum = max(
        (sum(abs(int(coefficient)) for coefficient in row) * max_input_magnitude for row in codes),
        default=0,
    )
    if maximum == 0:
        return input_width
    return max(input_width, ceil(log2(maximum + 1)) + 1)


def _pair_key(lhs: ValueRef, rhs: ValueRef) -> tuple[ValueRef, ValueRef]:
    if ref_sort_key(lhs) <= ref_sort_key(rhs):
        return lhs, rhs
    return rhs, lhs


def _count_adjacent_pairs(expressions: list[list[ValueRef]]) -> Counter[tuple[ValueRef, ValueRef]]:
    counts: Counter[tuple[ValueRef, ValueRef]] = Counter()
    for expression in expressions:
        ordered = sorted(expression, key=ref_sort_key)
        index = 0
        while index + 1 < len(ordered):
            lhs, rhs = ordered[index], ordered[index + 1]
            counts[_pair_key(lhs, rhs)] += 1
            index += 2 if lhs == rhs else 1
    return counts


def _replace_pair(
    expression: list[ValueRef],
    pair: tuple[ValueRef, ValueRef],
    replacement: NodeRef,
) -> tuple[list[ValueRef], int]:
    ordered = sorted(expression, key=ref_sort_key)
    replaced: list[ValueRef] = []
    replacement_count = 0
    index = 0
    while index < len(ordered):
        if index + 1 < len(ordered) and _pair_key(ordered[index], ordered[index + 1]) == pair:
            replaced.append(replacement)
            replacement_count += 1
            index += 2
        else:
            replaced.append(ordered[index])
            index += 1
    return sorted(replaced, key=ref_sort_key), replacement_count


def lower_codes(
    codes: NDArray[np.integer],
    *,
    input_width: int = 8,
    accumulator_width: int | None = None,
    enable_cse: bool = True,
    max_cse_nodes: int = 4096,
) -> CompilationPlan:
    """Compile a signed-power-of-two matrix into a hash-consed addition DAG."""

    matrix = np.asarray(codes, dtype=np.int64)
    if matrix.ndim != 2 or matrix.size == 0:
        raise ValueError("codes must be a non-empty 2-D matrix")
    if max_cse_nodes < 0:
        raise ValueError("max_cse_nodes must be non-negative")

    expressions: list[list[ValueRef]] = []
    for row in matrix:
        terms = [
            _coefficient_to_atom(column_index, int(coefficient))
            for column_index, coefficient in enumerate(row)
            if coefficient != 0
        ]
        expressions.append(sorted(terms, key=ref_sort_key))

    naive_add_count = sum(max(0, len(expression) - 1) for expression in expressions)
    nodes: list[AddNode] = []
    interned: dict[tuple[ValueRef, ValueRef], NodeRef] = {}
    depths: dict[ValueRef, int] = {}

    def depth_of(ref: ValueRef) -> int:
        return depths.get(ref, 0)

    def intern_add(lhs: ValueRef, rhs: ValueRef) -> NodeRef:
        pair = _pair_key(lhs, rhs)
        existing = interned.get(pair)
        if existing is not None:
            return existing
        node_id = len(nodes)
        reference = NodeRef(node_id)
        depth = max(depth_of(pair[0]), depth_of(pair[1])) + 1
        nodes.append(AddNode(node_id=node_id, lhs=pair[0], rhs=pair[1], depth=depth))
        interned[pair] = reference
        depths[reference] = depth
        return reference

    if enable_cse:
        blocked_pairs: set[tuple[ValueRef, ValueRef]] = set()
        for _ in range(max_cse_nodes):
            counts = _count_adjacent_pairs(expressions)
            candidates = [
                (count, pair)
                for pair, count in counts.items()
                if count >= 2 and pair not in blocked_pairs
            ]
            if not candidates:
                break
            _, best_pair = max(
                candidates,
                key=lambda item: (item[0], ref_sort_key(item[1][0]), ref_sort_key(item[1][1])),
            )
            node_ref = intern_add(*best_pair)
            replaced_total = 0
            updated: list[list[ValueRef]] = []
            for expression in expressions:
                replacement, count = _replace_pair(expression, best_pair, node_ref)
                updated.append(replacement)
                replaced_total += count
            if replaced_total < 2:
                blocked_pairs.add(best_pair)
                continue
            expressions = updated

    outputs: list[ValueRef | None] = []
    for expression in expressions:
        level = sorted(expression, key=ref_sort_key)
        if not level:
            outputs.append(None)
            continue
        while len(level) > 1:
            next_level: list[ValueRef] = []
            iterator = iter(level)
            for lhs in iterator:
                rhs = next(iterator, None)
                if rhs is None:
                    next_level.append(lhs)
                else:
                    next_level.append(intern_add(lhs, rhs))
            level = sorted(next_level, key=ref_sort_key)
        outputs.append(level[0])

    minimum_width = required_accumulator_width(matrix, input_width)
    chosen_width = minimum_width if accumulator_width is None else accumulator_width
    if chosen_width < minimum_width:
        raise ValueError(
            f"accumulator_width={chosen_width} is unsafe; "
            f"at least {minimum_width} bits are required"
        )

    return CompilationPlan(
        input_count=matrix.shape[1],
        output_count=matrix.shape[0],
        input_width=input_width,
        accumulator_width=chosen_width,
        nodes=tuple(nodes),
        outputs=tuple(outputs),
        naive_add_count=naive_add_count,
        cse_add_count=len(nodes),
        max_depth=max((node.depth for node in nodes), default=0),
    )
