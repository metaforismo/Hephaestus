import numpy as np
import pytest

from hephaestus.quantize import quantize_shift_add, shift_add_codebook


def test_three_bit_codebook_is_shift_add_friendly() -> None:
    assert shift_add_codebook(target_bits=3) == (-4, -2, -1, 0, 1, 2, 4)


def test_codebook_never_exceeds_index_capacity() -> None:
    assert shift_add_codebook(target_bits=2) == (-1, 0, 1)
    with pytest.raises(ValueError, match="needs more than 2 code bits"):
        shift_add_codebook(target_bits=2, max_shift=1)


def test_quantization_uses_only_codebook_and_power_of_two_scales() -> None:
    weights = np.array([[0.1, -0.7, 2.2], [0.0, 0.0, 0.0]], dtype=np.float64)
    result = quantize_shift_add(weights, target_bits=3)

    assert set(np.unique(result.codes)).issubset(set(result.codebook))
    assert result.row_scale_exponents.dtype == np.int64
    assert np.all(np.isfinite(result.dequantized()))
    assert result.weighted_mse >= 0


def test_importance_shape_is_checked() -> None:
    weights = np.ones((2, 3), dtype=np.float64)
    with pytest.raises(ValueError, match="one value per input"):
        quantize_shift_add(weights, importance=np.ones(4))
