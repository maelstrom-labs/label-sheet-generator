"""Unit conversion and the single rounding policy for the package.

Geometry is authored in millimetres or inches and rendered in PostScript
points. This module owns every conversion between them, so there is exactly one
place where a factor can be wrong.

Two rules the rest of the package depends on:

* Every converter rejects NaN and infinity. A non-finite dimension is the one
  input that silently corrupts geometry instead of raising: every comparison
  against NaN is ``False``, so bounds checks pass and the failure surfaces much
  later as an unreadable PDF or an HTTP 500.
* :func:`quantize` is the only rounding used when writing a value back to
  disk, and it keeps enough precision to round-trip real label stock. Avery
  5160's 2-5/8in label is 66.675mm; rounding to 2 decimals loses 5 microns per
  label and shifts the third column by a visible amount over a sheet.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4

#: Decimal places kept when serialising a length. Six is lossless for every
#: value that originates as an exact inch fraction converted to millimetres.
LENGTH_PRECISION = 6

_QUANTUM = Decimal(1).scaleb(-LENGTH_PRECISION)


def check_finite(value: float, name: str = "value") -> float:
    """Return ``value`` as a float, rejecting NaN and infinity.

    Raises:
        ValueError: if the value is not a finite real number.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number, not {numeric}")
    return numeric


def in_to_mm(value_in: float) -> float:
    """Convert inches to millimetres."""
    return check_finite(value_in, "value_in") * MM_PER_INCH


def mm_to_in(value_mm: float) -> float:
    """Convert millimetres to inches."""
    return check_finite(value_mm, "value_mm") / MM_PER_INCH


def mm_to_pt(value_mm: float) -> float:
    """Convert millimetres to PostScript points."""
    return check_finite(value_mm, "value_mm") * POINTS_PER_INCH / MM_PER_INCH


def pt_to_mm(value_pt: float) -> float:
    """Convert PostScript points to millimetres."""
    return check_finite(value_pt, "value_pt") * MM_PER_INCH / POINTS_PER_INCH


def in_to_pt(value_in: float) -> float:
    """Convert inches to PostScript points."""
    return check_finite(value_in, "value_in") * POINTS_PER_INCH


def pt_to_in(value_pt: float) -> float:
    """Convert PostScript points to inches."""
    return check_finite(value_pt, "value_pt") / POINTS_PER_INCH


def quantize(value: float, precision: int = LENGTH_PRECISION) -> float:
    """Round a length for serialisation, dropping float representation noise.

    ``66.67500000000001`` becomes ``66.675``; ``-0.0`` becomes ``0.0`` so a
    written template never contains a negative zero.
    """
    numeric = check_finite(value, "value")
    quantum = _QUANTUM if precision == LENGTH_PRECISION else Decimal(1).scaleb(-precision)
    try:
        rounded = float(Decimal(repr(numeric)).quantize(quantum, rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:  # pragma: no cover - guarded by check_finite
        raise ValueError(f"cannot quantize {numeric}") from exc
    return 0.0 if rounded == 0 else rounded
