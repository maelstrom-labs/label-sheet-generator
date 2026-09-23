"""The trust boundary: parsing, validation, and lossless round-tripping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from label_sheet_generator.schema import (
    BarcodeElement,
    ImageElement,
    LabelTemplate,
    TextElement,
    TextLayoutTemplate,
    parse_field_references,
    render_template_string,
)

DATA = Path(__file__).resolve().parents[1] / "src/label_sheet_generator/data/templates"


def load(relative: str) -> dict:
    return json.loads((DATA / relative).read_text())


# --- the regression that motivated the rewrite ----------------------------


def test_every_element_type_serialises() -> None:
    # The old dataclass hierarchy raised TypeError from super().to_dict() for
    # all three subclasses, because @dataclass(slots=True) returns a new class
    # while the __class__ cell captured by zero-arg super() points at the old
    # one. That made save_template fail for any template with any element.
    for element in (
        TextElement(x_mm=1, y_mm=2, width_mm=10, height_mm=5, field="name"),
        BarcodeElement(x_mm=1, y_mm=2, width_mm=10, height_mm=5, field="sku"),
        ImageElement(x_mm=1, y_mm=2, width_mm=10, height_mm=5, field="logo"),
    ):
        payload = element.dump("mm")
        assert payload["type"] == element.type
        assert payload["x_mm"] == 1


def test_label_template_with_elements_round_trips() -> None:
    template = LabelTemplate.model_validate(load("labels/basic-address.json"))
    assert LabelTemplate.model_validate(template.dump()).dump() == template.dump()


# --- unit preservation ----------------------------------------------------


def test_inch_authored_template_stays_in_inches() -> None:
    template = LabelTemplate.model_validate(load("labels/spice-jar.json"))
    assert template.units == "in"
    dumped = template.dump()
    assert "width_in" in dumped["page"]
    assert "width_mm" not in dumped["page"]
    assert dumped["page"]["width_in"] == 1.35


def test_mm_authored_template_stays_in_mm() -> None:
    template = LabelTemplate.model_validate(load("labels/basic-address.json"))
    assert template.units == "mm"
    assert "width_mm" in template.dump()["page"]


def test_inches_convert_exactly() -> None:
    template = LabelTemplate.model_validate(
        {"page": {"width_in": 2.625, "height_in": 1}, "grid": {"rows": 1, "cols": 1}}
    )
    assert template.page.width_mm == pytest.approx(66.675)


def test_declaring_both_units_for_one_value_is_an_error() -> None:
    with pytest.raises(PydanticValidationError, match="not both"):
        LabelTemplate.model_validate(
            {
                "page": {"width_mm": 100, "width_in": 4, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
            }
        )


# --- structural rejection -------------------------------------------------


@pytest.mark.parametrize(
    "document,reason",
    [
        (
            {"page": {"width_mm": float("nan"), "height_mm": 100}, "grid": {"rows": 1, "cols": 1}},
            "NaN",
        ),
        ({"page": {"width_mm": 0, "height_mm": 100}, "grid": {"rows": 1, "cols": 1}}, "zero width"),
        ({"page": {"width_mm": -5, "height_mm": 100}, "grid": {"rows": 1, "cols": 1}}, "negative"),
        (
            {"page": {"width_mm": 100, "height_mm": 100}, "grid": {"rows": 0, "cols": 1}},
            "zero rows",
        ),
        (
            {"page": {"width_mm": 100, "height_mm": 100}, "grid": {"rows": True, "cols": 1}},
            "bool rows",
        ),
        (
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "nope": 1,
            },
            "unknown key",
        ),
        ({"page": "width_mm", "grid": {"rows": 1, "cols": 1}}, "page is a string"),
        ({"page": {"width_mm": 100, "height_mm": 100}, "grid": [1, 2]}, "grid is a list"),
        ({"page": {"width_mm": 100, "height_mm": 100}, "grid": {"cols": 1}}, "missing rows"),
        (
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": ["x"],
            },
            "element is a string",
        ),
        (
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": 42,
            },
            "elements not a list",
        ),
    ],
)
def test_malformed_documents_raise_one_typed_error(document: dict, reason: str) -> None:
    # Every one of these produced a different uncaught builtin exception in the
    # old parser -- KeyError, TypeError, AttributeError, ZeroDivisionError --
    # each of which becomes an HTTP 500 at the API boundary.
    with pytest.raises(PydanticValidationError):
        LabelTemplate.model_validate(document)


def test_element_cannot_declare_two_content_sources() -> None:
    with pytest.raises(PydanticValidationError, match="exactly one content source"):
        TextElement(x_mm=0, y_mm=0, field="name", value="literal")


@pytest.mark.parametrize("element_type", ["barcode", "image"])
def test_barcode_and_image_require_a_box(element_type: str) -> None:
    with pytest.raises(PydanticValidationError, match="require a width and a height"):
        LabelTemplate.model_validate(
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": [{"type": element_type, "x_mm": 1, "y_mm": 1}],
            }
        )


def test_unknown_element_type_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        LabelTemplate.model_validate(
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": [{"type": "qrcode", "x_mm": 1, "y_mm": 1}],
            }
        )


# --- template strings -----------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "{a.__class__}",
        "{0.__class__.__mro__}",
        "{a[0]}",
        "{a!r}",
        "{a:>100000000}",
        "{}",
        "{0}",
    ],
)
def test_format_string_escapes_are_rejected_at_parse_time(hostile: str) -> None:
    # str.format_map on a user string walks attributes and can allocate
    # unbounded padding. The grammar only permits a bare field name.
    with pytest.raises(ValueError):
        parse_field_references(hostile)


def test_plain_references_are_accepted() -> None:
    assert parse_field_references("{address_1}\n{address_2}") == ["address_1", "address_2"]


def test_unmatched_brace_is_rejected() -> None:
    with pytest.raises(ValueError, match="unmatched brace"):
        parse_field_references("{name")


def test_substitution_blanks_unknown_fields_and_honours_escapes() -> None:
    assert render_template_string("{a}-{b}", {"a": "1"}) == "1-"
    assert render_template_string("{{literal}} {name}", {"name": "Ada"}) == "{literal} Ada"


# --- field discovery ------------------------------------------------------


def test_field_names_hoist_name_and_deduplicate() -> None:
    template = TextLayoutTemplate.model_validate(
        {
            "template_type": "text-layout",
            "elements": [
                {"type": "text", "x_mm": 0, "y_mm": 0, "field": "sku"},
                {"type": "text", "x_mm": 0, "y_mm": 0, "template": "{a}\n{b}"},
                {"type": "text", "x_mm": 0, "y_mm": 0, "field": "name"},
                {"type": "text", "x_mm": 0, "y_mm": 0, "field": "sku"},
            ],
        }
    )
    assert template.field_names == ["name", "sku", "a", "b"]


def test_models_are_frozen() -> None:
    template = LabelTemplate.model_validate(load("labels/basic-address.json"))
    with pytest.raises(PydanticValidationError):
        template.name = "changed"  # type: ignore[misc]
