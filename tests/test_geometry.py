"""Slot arithmetic and the two-directional validation fix."""

from __future__ import annotations

import pytest

from label_sheet_generator.errors import GeometryError
from label_sheet_generator.geometry import compute_slots, resolve_label_size_mm, validate
from label_sheet_generator.schema import LabelTemplate
from label_sheet_generator.units import mm_to_pt


def template(**grid: object) -> LabelTemplate:
    base = {"rows": 2, "cols": 2}
    base.update(grid)
    return LabelTemplate.model_validate({"page": {"width_mm": 100, "height_mm": 100}, "grid": base})


def test_derived_label_size_accounts_for_all_four_margins_and_gaps() -> None:
    width, height = resolve_label_size_mm(
        template(
            margin_left_mm=10,
            margin_right_mm=10,
            gap_x_mm=10,
            margin_top_mm=5,
            margin_bottom_mm=5,
            gap_y_mm=0,
        )
    )
    assert width == pytest.approx((100 - 10 - 10 - 10) / 2)
    assert height == pytest.approx((100 - 5 - 5) / 2)


def test_a_grid_that_fits_exactly_is_valid() -> None:
    # The old code compared against page width with a bare 0.01 fudge and
    # could reject a grid that fit precisely.
    report = validate(template(label_width_mm=50, label_height_mm=50))
    assert report.ok, report.errors


def test_grid_overrunning_the_page_is_an_error() -> None:
    report = validate(template(label_width_mm=60, label_height_mm=50))
    assert not report.ok
    assert report.errors[0].code == "grid_exceeds_page"
    assert report.errors[0].axis == "x"
    assert report.errors[0].overflow_mm == pytest.approx(20)


def test_grid_overrunning_a_declared_margin_is_a_warning_not_silence() -> None:
    # margin_right_mm and margin_bottom_mm were parsed and then never read, so
    # a grid could silently ignore the margins its own author declared.
    report = validate(
        template(label_width_mm=50, label_height_mm=50, margin_right_mm=50, margin_bottom_mm=50)
    )
    assert report.ok
    codes = {issue.code for issue in report.warnings}
    assert codes == {"grid_exceeds_margin"}
    assert {issue.axis for issue in report.warnings} == {"x", "y"}


def test_margins_consuming_the_page_are_reported_not_crashed() -> None:
    report = validate(template(margin_left_mm=60, margin_right_mm=60))
    assert not report.ok
    assert report.errors[0].code == "grid_does_not_fit"


def test_element_outside_its_label_warns_with_the_overflow() -> None:
    doc = LabelTemplate.model_validate(
        {
            "page": {"width_mm": 100, "height_mm": 100},
            "grid": {"rows": 1, "cols": 1, "label_width_mm": 50, "label_height_mm": 50},
            "elements": [{"type": "text", "x_mm": 40, "y_mm": 0, "width_mm": 30, "height_mm": 10}],
        }
    )
    report = validate(doc)
    assert report.ok
    assert report.warnings[0].code == "element_exceeds_label"
    assert report.warnings[0].overflow_mm == pytest.approx(20)


def test_slots_are_in_reading_order_with_the_origin_at_the_bottom_left() -> None:
    slots = compute_slots(
        template(
            label_width_mm=40,
            label_height_mm=40,
            margin_left_mm=10,
            margin_top_mm=10,
            gap_x_mm=0,
            gap_y_mm=0,
        )
    )
    assert [(slot.row, slot.col) for slot in slots] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    # PDF y grows upward, so the first row sits nearer the top of the page.
    assert slots[0].y_pt > slots[2].y_pt
    assert slots[0].x_pt == pytest.approx(mm_to_pt(10))
    assert slots[0].y_pt == pytest.approx(mm_to_pt(100 - 10 - 40))


def test_compute_slots_refuses_a_broken_grid() -> None:
    with pytest.raises(GeometryError):
        compute_slots(template(label_width_mm=60, label_height_mm=50))


def test_slot_count_matches_the_grid() -> None:
    assert len(compute_slots(template(rows=10, cols=3, label_width_mm=30, label_height_mm=9))) == 30
