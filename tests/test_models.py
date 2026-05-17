from math import isclose

import pytest

from label_sheet_generator.models import TemplateError, parse_template, parse_text_layout_template
from label_sheet_generator.units import in_to_mm


def test_parse_template_accepts_inch_geometry() -> None:
    template = parse_template(
        {
            "name": "inch-template",
            "page": {
                "width_in": 8.5,
                "height_in": 11,
            },
            "grid": {
                "rows": 10,
                "cols": 3,
                "margin_left_in": 0.1875,
                "margin_top_in": 0.5,
                "margin_right_in": 0.1875,
                "margin_bottom_in": 0.5,
                "gap_x_in": 0.125,
                "gap_y_in": 0,
                "label_width_in": 2.625,
                "label_height_in": 1,
            },
            "elements": [
                {
                    "type": "text",
                    "field": "name",
                    "x_in": 0.125,
                    "y_in": 0.125,
                    "width_in": 2,
                    "height_in": 0.4,
                }
            ],
        }
    )

    assert isclose(template.page.width_mm, in_to_mm(8.5))
    assert isclose(template.page.height_mm, in_to_mm(11.0))
    assert isclose(template.grid.margin_left_mm, in_to_mm(0.1875))
    assert isclose(template.grid.label_width_mm, in_to_mm(2.625))
    assert isclose(template.elements[0].x_mm, in_to_mm(0.125))
    assert isclose(template.elements[0].width_mm, in_to_mm(2.0))


def test_parse_template_rejects_mixed_units_for_one_value() -> None:
    with pytest.raises(TemplateError, match="page.width accepts either width_mm or width_in, not both"):
        parse_template(
            {
                "page": {
                    "width_mm": 215.9,
                    "width_in": 8.5,
                    "height_mm": 279.4,
                },
                "grid": {
                    "rows": 1,
                    "cols": 1,
                },
            }
        )


def test_parse_text_layout_template_accepts_elements_only() -> None:
    layout = parse_text_layout_template(
        {
            "template_type": "text-layout",
            "name": "address-layout",
            "elements": [
                {
                    "type": "text",
                    "field": "name",
                    "x_mm": 4,
                    "y_mm": 3,
                    "width_mm": 58,
                    "height_mm": 8,
                    "align": "center",
                    "rotation_deg": 90,
                }
            ],
        }
    )

    assert layout.name == "address-layout"
    assert len(layout.elements) == 1
    assert isclose(layout.elements[0].x_mm, 4.0)
    assert layout.elements[0].align == "center"
    assert isclose(layout.elements[0].rotation_deg, 90.0)


def test_parse_text_layout_template_rejects_invalid_align() -> None:
    with pytest.raises(TemplateError, match="element.align must be one of: left, center, right, justify"):
        parse_text_layout_template(
            {
                "template_type": "text-layout",
                "elements": [
                    {
                        "type": "text",
                        "field": "name",
                        "x_mm": 4,
                        "y_mm": 3,
                        "width_mm": 58,
                        "height_mm": 8,
                        "align": "diagonal",
                    }
                ],
            }
        )