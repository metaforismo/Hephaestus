"""Qualified sequential equivalence for routed registered tiles."""

from ._builder import build_evidence
from ._common import PostPhysicalEquivalenceError
from ._proof import (
    emit_bounded_reset_script,
    emit_equivalence_script,
    emit_fault_wrapper,
    emit_passthrough_wrapper,
)

__all__ = [
    "PostPhysicalEquivalenceError",
    "build_evidence",
    "emit_bounded_reset_script",
    "emit_equivalence_script",
    "emit_fault_wrapper",
    "emit_passthrough_wrapper",
]
