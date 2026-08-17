"""Hardware-aware power-of-two quantization."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, log2

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


@dataclass(frozen=True)
class QuantizedMatrix:
    """A matrix represented by power-of-two integer codes and row scales."""

    codes: IntArray
    row_scale_exponents: IntArray
    codebook: tuple[int, ...]
    target_bits: int
    max_shift: int
    weighted_mse: float

    @property
    def storage_bits(self) -> int:
        return max(1, ceil(log2(len(self.codebook))))

    def dequantized(self) -> FloatArray:
        scales = np.exp2(self.row_scale_exponents.astype(np.float64))[:, None]
        return self.codes.astype(np.float64) * scales


def shift_add_codebook(*, target_bits: int, max_shift: int | None = None) -> tuple[int, ...]:
    """Build a zero plus signed powers-of-two codebook.

    ``target_bits`` limits the number of codebook entries. ``max_shift`` can be
    lower than that limit to trade dynamic range for denser quantization levels.
    """

    if target_bits < 2 or target_bits > 8:
        raise ValueError("target_bits must be in [2, 8]")
    capacity_max_shift = (((1 << target_bits) - 1) // 2) - 1
    if max_shift is None:
        max_shift = min(target_bits - 1, capacity_max_shift)
    if max_shift < 0:
        raise ValueError("max_shift must be non-negative")
    if max_shift > capacity_max_shift:
        raise ValueError(
            f"max_shift={max_shift} needs more than {target_bits} code bits; "
            f"maximum is {capacity_max_shift}"
        )

    magnitudes = tuple(1 << shift for shift in range(max_shift + 1))
    negative = tuple(sorted(-value for value in magnitudes))
    return negative + (0,) + magnitudes


def _nearest_codes(normalized: FloatArray, codebook: NDArray[np.float64]) -> IntArray:
    distances = np.abs(normalized[..., None] - codebook)
    indices = np.argmin(distances, axis=-1)
    return codebook[indices].astype(np.int64)


def _importance_matrix(weights: FloatArray, importance: NDArray[np.float64] | None) -> FloatArray:
    if importance is None:
        return np.ones_like(weights, dtype=np.float64)

    importance_array = np.asarray(importance, dtype=np.float64)
    if importance_array.ndim == 1:
        if importance_array.shape[0] != weights.shape[1]:
            raise ValueError("1-D importance must have one value per input column")
        importance_array = np.broadcast_to(importance_array[None, :], weights.shape)
    elif importance_array.shape != weights.shape:
        raise ValueError("importance must have shape (inputs,) or the same shape as weights")

    if np.any(importance_array < 0) or not np.all(np.isfinite(importance_array)):
        raise ValueError("importance values must be finite and non-negative")
    return np.asarray(importance_array, dtype=np.float64)


def quantize_shift_add(
    weights: NDArray[np.floating] | NDArray[np.integer],
    *,
    target_bits: int = 3,
    max_shift: int | None = None,
    importance: NDArray[np.floating] | None = None,
    exponent_search_radius: int = 4,
) -> QuantizedMatrix:
    """Quantize a 2-D matrix to signed powers of two with power-of-two row scales."""

    matrix = np.asarray(weights, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("weights must be a 2-D matrix")
    if matrix.size == 0:
        raise ValueError("weights must not be empty")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("weights must contain only finite values")
    if exponent_search_radius < 0:
        raise ValueError("exponent_search_radius must be non-negative")

    codebook_tuple = shift_add_codebook(target_bits=target_bits, max_shift=max_shift)
    effective_max_shift = max(abs(value).bit_length() - 1 for value in codebook_tuple if value)
    codebook = np.asarray(codebook_tuple, dtype=np.float64)
    sensitivity = _importance_matrix(matrix, importance)

    codes = np.zeros(matrix.shape, dtype=np.int64)
    exponents = np.zeros(matrix.shape[0], dtype=np.int64)
    weighted_error = 0.0
    total_weight = float(np.sum(sensitivity))

    max_code = float(np.max(np.abs(codebook)))
    for row_index, row in enumerate(matrix):
        row_importance = sensitivity[row_index]
        max_abs = float(np.max(np.abs(row)))
        if max_abs == 0.0:
            continue

        central_exponent = int(np.floor(np.log2(max_abs / max_code)))
        best_error = float("inf")
        best_codes: IntArray | None = None
        best_exponent = central_exponent

        for exponent in range(
            central_exponent - exponent_search_radius,
            central_exponent + exponent_search_radius + 1,
        ):
            scale = float(2.0**exponent)
            candidate_codes = _nearest_codes(row / scale, codebook)
            reconstructed = candidate_codes.astype(np.float64) * scale
            error = float(np.sum(row_importance * np.square(row - reconstructed)))
            if error < best_error:
                best_error = error
                best_codes = candidate_codes
                best_exponent = exponent

        if best_codes is None:  # pragma: no cover - loop is non-empty by construction
            raise RuntimeError("quantizer failed to select a code assignment")
        codes[row_index] = best_codes
        exponents[row_index] = best_exponent
        weighted_error += best_error

    normalized_mse = weighted_error / total_weight if total_weight > 0 else 0.0
    return QuantizedMatrix(
        codes=codes,
        row_scale_exponents=exponents,
        codebook=codebook_tuple,
        target_bits=target_bits,
        max_shift=effective_max_shift,
        weighted_mse=normalized_mse,
    )
