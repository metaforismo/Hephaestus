"""Serializable intermediate representation for constant matrix-vector circuits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, order=True)
class Atom:
    """A signed power-of-two multiple of one activation input."""

    input_index: int
    shift: int
    sign: int

    def __post_init__(self) -> None:
        if self.input_index < 0:
            raise ValueError("input_index must be non-negative")
        if self.shift < 0:
            raise ValueError("shift must be non-negative")
        if self.sign not in (-1, 1):
            raise ValueError("sign must be -1 or +1")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "kind": "atom",
            "input_index": self.input_index,
            "shift": self.shift,
            "sign": self.sign,
        }


@dataclass(frozen=True, order=True)
class NodeRef:
    """Reference to an addition node."""

    node_id: int

    def __post_init__(self) -> None:
        if self.node_id < 0:
            raise ValueError("node_id must be non-negative")

    def to_dict(self) -> dict[str, int | str]:
        return {"kind": "node", "node_id": self.node_id}


ValueRef = Atom | NodeRef


def ref_sort_key(ref: ValueRef) -> tuple[int, int, int, int]:
    """Return a stable canonical ordering for DAG operands."""

    if isinstance(ref, Atom):
        return (0, ref.input_index, ref.shift, ref.sign)
    return (1, ref.node_id, 0, 0)


@dataclass(frozen=True)
class AddNode:
    """One combinational addition in the compiled DAG."""

    node_id: int
    lhs: ValueRef
    rhs: ValueRef
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "lhs": ref_to_dict(self.lhs),
            "rhs": ref_to_dict(self.rhs),
            "depth": self.depth,
        }


@dataclass(frozen=True)
class CompilationPlan:
    """A whole constant matrix-vector circuit after topology lowering."""

    input_count: int
    output_count: int
    input_width: int
    accumulator_width: int
    nodes: tuple[AddNode, ...]
    outputs: tuple[ValueRef | None, ...]
    naive_add_count: int
    cse_add_count: int
    max_depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "hephaestus.plan.v1",
            "input_count": self.input_count,
            "output_count": self.output_count,
            "input_width": self.input_width,
            "accumulator_width": self.accumulator_width,
            "nodes": [node.to_dict() for node in self.nodes],
            "outputs": [None if ref is None else ref_to_dict(ref) for ref in self.outputs],
            "metrics": {
                "naive_add_count": self.naive_add_count,
                "cse_add_count": self.cse_add_count,
                "shared_additions_saved": self.naive_add_count - self.cse_add_count,
                "max_depth": self.max_depth,
                "runtime_weight_reads_per_matvec": 0,
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompilationPlan:
        if value.get("schema") != "hephaestus.plan.v1":
            raise ValueError("unsupported plan schema")
        metrics = value["metrics"]
        nodes = tuple(
            AddNode(
                node_id=int(node["node_id"]),
                lhs=ref_from_dict(node["lhs"]),
                rhs=ref_from_dict(node["rhs"]),
                depth=int(node["depth"]),
            )
            for node in value["nodes"]
        )
        outputs = tuple(None if ref is None else ref_from_dict(ref) for ref in value["outputs"])
        return cls(
            input_count=int(value["input_count"]),
            output_count=int(value["output_count"]),
            input_width=int(value["input_width"]),
            accumulator_width=int(value["accumulator_width"]),
            nodes=nodes,
            outputs=outputs,
            naive_add_count=int(metrics["naive_add_count"]),
            cse_add_count=int(metrics["cse_add_count"]),
            max_depth=int(metrics["max_depth"]),
        )


def ref_to_dict(ref: ValueRef) -> dict[str, int | str]:
    return ref.to_dict()


def ref_from_dict(value: dict[str, Any]) -> ValueRef:
    kind: Literal["atom", "node"] | None = value.get("kind")
    if kind == "atom":
        return Atom(
            input_index=int(value["input_index"]),
            shift=int(value["shift"]),
            sign=int(value["sign"]),
        )
    if kind == "node":
        return NodeRef(node_id=int(value["node_id"]))
    raise ValueError(f"unsupported value reference kind: {kind!r}")
