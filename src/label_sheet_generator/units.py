POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4


def in_to_mm(value_in: float) -> float:
    return value_in * MM_PER_INCH


def mm_to_pt(value_mm: float) -> float:
    return value_mm * POINTS_PER_INCH / MM_PER_INCH


def pt_to_mm(value_pt: float) -> float:
    return value_pt * MM_PER_INCH / POINTS_PER_INCH
