import numpy as np
import pytest

from hephaestus.lower import lower_codes
from hephaestus.reference import evaluate_codes, evaluate_plan


def test_lowered_plan_is_bit_exact() -> None:
    codes = np.array(
        [
            [1, -2, 0, 4],
            [1, -2, 4, 0],
            [0, 0, 0, 0],
        ],
        dtype=np.int64,
    )
    plan = lower_codes(codes, input_width=8)
    rng = np.random.default_rng(5)

    for _ in range(100):
        vector = rng.integers(-128, 128, size=4, dtype=np.int64)
        assert np.array_equal(evaluate_plan(plan, vector), evaluate_codes(codes, vector))


def test_global_cse_shares_repeated_partial_sums() -> None:
    codes = np.array([[1, 2, 0], [1, 2, 4]], dtype=np.int64)
    plan = lower_codes(codes, input_width=8, enable_cse=True)

    assert plan.naive_add_count == 3
    assert plan.cse_add_count == 2
    assert plan.max_depth == 2


def test_non_power_of_two_coefficients_are_rejected() -> None:
    with pytest.raises(ValueError, match="not a signed power of two"):
        lower_codes(np.array([[3]], dtype=np.int64))


def test_reference_evaluation_does_not_overflow_int64() -> None:
    codes = np.array([[1 << 62]], dtype=np.int64)
    vector = np.array([-128], dtype=np.int64)
    plan = lower_codes(codes, input_width=8)

    expected = -(1 << 69)
    assert evaluate_codes(codes, vector).tolist() == [expected]
    assert evaluate_plan(plan, vector).tolist() == [expected]
