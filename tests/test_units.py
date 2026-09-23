"""Unit conversion and the rounding policy."""

from __future__ import annotations

import math

import pytest

from label_sheet_generator.units import (
    MM_PER_INCH,
    check_finite,
    in_to_mm,
    mm_to_in,
    mm_to_pt,
    pt_to_mm,
    quantize,
)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_every_converter_rejects_non_finite(bad: float) -> None:
    # A NaN dimension defeats every bounds check downstream, because each
    # comparison against it is False. It has to die at the conversion.
    for convert in (in_to_mm, mm_to_in, mm_to_pt, pt_to_mm):
        with pytest.raises(ValueError, match="finite"):
            convert(bad)


def test_check_finite_rejects_non_numbers() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        check_finite("wide")  # type: ignore[arg-type]


def test_round_trip_is_exact_enough_for_print() -> None:
    for value in (0.0, 1.0, 25.4, 66.675, 215.9, 279.4):
        assert mm_to_in(in_to_mm(value)) == pytest.approx(value, abs=1e-12)
        assert pt_to_mm(mm_to_pt(value)) == pytest.approx(value, abs=1e-12)


def test_inch_to_mm_uses_the_exact_factor() -> None:
    assert in_to_mm(1) == MM_PER_INCH
    assert in_to_mm(2.625) == pytest.approx(66.675)


def test_quantize_drops_float_noise_without_losing_real_precision() -> None:
    # Avery 5160 is 2-5/8in == 66.675mm exactly. Rounding to 2dp, as the old
    # writer did, shifts the third column of a sheet by a visible amount.
    assert quantize(66.67500000000001) == 66.675
    assert quantize(66.675) == 66.675


def test_quantize_normalises_negative_zero() -> None:
    assert quantize(-0.0) == 0.0
    assert not math.copysign(1, quantize(-0.0)) < 0
