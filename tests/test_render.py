"""Rendering: bytes in memory, attributed failures, and refused limits."""

from __future__ import annotations

import inspect
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from label_sheet_generator.assets import AssetLoader
from label_sheet_generator.errors import (
    AssetError,
    GeometryError,
    LimitExceeded,
    RecordError,
    RenderError,
    ValidationError,
)
from label_sheet_generator.geometry import Slot
from label_sheet_generator.render import (
    RenderOptions,
    RenderResult,
    page_count,
    render_page_png,
    render_pdf,
)
from label_sheet_generator.render.barcode import ean13_check_digit
from label_sheet_generator.render.text import element_box_pt, parse_color, resolve_content
from label_sheet_generator.schema import LabelTemplate, TextElement
from label_sheet_generator.units import mm_to_pt

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: A 2x2 grid on A4, so one page holds four labels and five records spill.
PAGE = {"width_mm": 210.0, "height_mm": 297.0}
GRID = {
    "rows": 2,
    "cols": 2,
    "label_width_mm": 90.0,
    "label_height_mm": 50.0,
    "margin_left_mm": 10.0,
    "margin_top_mm": 10.0,
    "gap_x_mm": 5.0,
    "gap_y_mm": 5.0,
}
CELLS_PER_PAGE = GRID["rows"] * GRID["cols"]

TEXT_ELEMENT: dict[str, Any] = {
    "type": "text",
    "x_mm": 2.0,
    "y_mm": 2.0,
    "width_mm": 80.0,
    "height_mm": 12.0,
    "field": "name",
}


def template(*elements: dict[str, Any], **grid: Any) -> LabelTemplate:
    """A valid label template carrying the given elements."""
    return LabelTemplate.model_validate(
        {"page": dict(PAGE), "grid": {**GRID, **grid}, "elements": list(elements)}
    )


def text_template(**overrides: Any) -> LabelTemplate:
    return template({**TEXT_ELEMENT, **overrides})


def barcode_element(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "barcode",
        "x_mm": 2.0,
        "y_mm": 2.0,
        "width_mm": 60.0,
        "height_mm": 25.0,
        "barcode_type": "ean13",
        **overrides,
    }


def image_element(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "image",
        "x_mm": 2.0,
        "y_mm": 2.0,
        "width_mm": 20.0,
        "height_mm": 20.0,
        "value": "logo.png",
        **overrides,
    }


def named_records(count: int) -> list[dict[str, Any]]:
    return [{"name": f"record-{index}"} for index in range(count)]


def pages(result: RenderResult) -> list[Any]:
    return PdfReader(io.BytesIO(result.pdf_bytes)).pages


def text_of(result: RenderResult, page: int = 0) -> str:
    return pages(result)[page].extract_text()


def slot() -> Slot:
    return Slot(index=0, row=0, col=0, x_pt=0.0, y_pt=0.0, width_pt=200.0, height_pt=100.0)


@pytest.fixture
def loader(asset_dir: Path) -> AssetLoader:
    """An enabled asset loader over the conftest asset root."""
    return AssetLoader(asset_dir, max_bytes=1_000_000, max_pixels=10_000_000)


# --- document shape -------------------------------------------------------


def test_render_returns_pdf_bytes_rather_than_a_file() -> None:
    result = render_pdf(text_template(), named_records(2))
    assert isinstance(result.pdf_bytes, bytes)
    assert result.pdf_bytes.startswith(b"%PDF")


@pytest.mark.parametrize(
    ("record_count", "expected_pages", "expected_labels"),
    [
        (0, 1, CELLS_PER_PAGE),
        (1, 1, 1),
        (CELLS_PER_PAGE - 1, 1, CELLS_PER_PAGE - 1),
        (CELLS_PER_PAGE, 1, CELLS_PER_PAGE),
        (CELLS_PER_PAGE + 1, 2, CELLS_PER_PAGE + 1),
        (2 * CELLS_PER_PAGE + 1, 3, 2 * CELLS_PER_PAGE + 1),
    ],
)
def test_page_and_label_counts_follow_the_record_count(
    record_count: int, expected_pages: int, expected_labels: int
) -> None:
    result = render_pdf(text_template(), named_records(record_count))
    assert result.page_count == expected_pages
    assert result.label_count == expected_labels


def test_no_records_renders_one_page_of_static_labels() -> None:
    # The old code approximated this with `records or [{}]`, which drew a
    # single label on an otherwise blank sheet instead of a full page.
    result = render_pdf(template({**TEXT_ELEMENT, "field": None, "value": "STATIC"}), [])
    assert result.page_count == 1
    assert text_of(result).count("STATIC") == CELLS_PER_PAGE


def test_reported_page_count_matches_the_pages_in_the_document() -> None:
    result = render_pdf(text_template(), named_records(CELLS_PER_PAGE + 1))
    assert len(pages(result)) == result.page_count


def test_preview_page_count_agrees_with_the_render_result() -> None:
    result = render_pdf(text_template(), named_records(CELLS_PER_PAGE + 2))
    assert page_count(result.pdf_bytes) == result.page_count


def test_records_beyond_the_first_page_are_drawn_on_the_second() -> None:
    result = render_pdf(text_template(), named_records(CELLS_PER_PAGE + 1))
    first, second = text_of(result, 0), text_of(result, 1)
    assert "record-0" in first
    assert f"record-{CELLS_PER_PAGE}" not in first
    assert f"record-{CELLS_PER_PAGE}" in second


def test_the_packaged_basic_address_template_renders(
    catalog: Any, basic_template_id: str, sample_document: str
) -> None:
    entry = catalog.get(basic_template_id)
    records = json.loads(sample_document)["records"]
    result = render_pdf(entry.template, records)
    assert result.warnings == []
    assert result.page_count == 1
    assert "Ada Lovelace" in text_of(result)


# --- nothing touches the filesystem ---------------------------------------


def test_render_takes_no_output_path_argument() -> None:
    # The old signature took an output_path, wrote it, reopened it to apply
    # rotation and rewrote it, so a failure left a stale PDF in place.
    parameters = inspect.signature(render_pdf).parameters
    assert not [name for name in parameters if "path" in name or "output_file" in name]


def test_render_writes_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    render_pdf(text_template(), named_records(CELLS_PER_PAGE + 1))
    assert set(tmp_path.rglob("*")) == before


# --- presentation options -------------------------------------------------


def test_landscape_swaps_the_page_dimensions() -> None:
    portrait = pages(render_pdf(text_template(), named_records(1)))[0].mediabox
    landscape = pages(
        render_pdf(
            text_template(),
            named_records(1),
            options=RenderOptions(page_orientation="landscape"),
        )
    )[0].mediabox
    assert float(landscape.width) == pytest.approx(float(portrait.height))
    assert float(landscape.height) == pytest.approx(float(portrait.width))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_rotation_is_written_as_the_page_rotate_attribute(rotation: int) -> None:
    # Rotation used to be a post-process: save the PDF, reopen it with pypdf,
    # set /Rotate and rewrite. It is a page attribute reportlab can set, so
    # nothing re-reads the document now.
    result = render_pdf(
        text_template(),
        named_records(CELLS_PER_PAGE + 1),
        options=RenderOptions(page_rotation_deg=rotation),
    )
    assert [page.get("/Rotate") for page in pages(result)] == [rotation, rotation]


@pytest.mark.parametrize(
    ("field", "value"),
    [("page_orientation", "sideways"), ("page_rotation_deg", 45), ("page_rotation_deg", 360)],
)
def test_an_out_of_range_option_is_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        render_pdf(text_template(), named_records(1), options=RenderOptions(**{field: value}))
    assert caught.value.loc == (field,)


@pytest.mark.parametrize("inset_mm", [0.0, -1.0, 30.0, 200.0])
def test_a_bleed_inset_that_leaves_no_area_is_rejected(inset_mm: float) -> None:
    with pytest.raises(GeometryError):
        render_pdf(
            text_template(),
            named_records(1),
            options=RenderOptions(bleed_guide_inset_mm=inset_mm),
        )


@pytest.mark.parametrize(
    "options",
    [
        RenderOptions(outline_slots=True),
        RenderOptions(bleed_guide_inset_mm=2.0),
        RenderOptions(text_rotation_deg=90.0),
    ],
)
def test_guides_and_rotation_change_the_drawn_page(options: RenderOptions) -> None:
    plain = render_pdf(text_template(), named_records(1))
    decorated = render_pdf(text_template(), named_records(1), options=options)
    assert decorated.warnings == []
    assert decorated.pdf_bytes != plain.pdf_bytes


# --- limits ---------------------------------------------------------------


def test_max_pages_is_refused_before_anything_is_drawn() -> None:
    # The template below fails on its first element, so a LimitExceeded here
    # (rather than the font error) proves the limit is checked up front.
    doomed = text_template(font_name="Helvitica")
    with pytest.raises(LimitExceeded) as caught:
        render_pdf(
            doomed,
            named_records(CELLS_PER_PAGE + 1),
            options=RenderOptions(strict=True),
            max_pages=1,
        )
    assert caught.value.limit_name == "max_pages"


def test_max_pages_reports_the_limit_and_the_page_count_requested() -> None:
    with pytest.raises(LimitExceeded) as caught:
        render_pdf(text_template(), named_records(3 * CELLS_PER_PAGE), max_pages=2)
    assert caught.value.limit == 2
    assert caught.value.actual == 3


def test_a_render_at_exactly_the_page_limit_is_allowed() -> None:
    result = render_pdf(text_template(), named_records(2 * CELLS_PER_PAGE), max_pages=2)
    assert result.page_count == 2


def test_max_output_bytes_refuses_an_oversized_document() -> None:
    with pytest.raises(LimitExceeded) as caught:
        render_pdf(text_template(), named_records(1), max_output_bytes=16)
    assert caught.value.limit_name == "max_output_bytes"
    assert caught.value.actual is not None
    assert caught.value.actual > 16


def test_max_output_bytes_allows_a_document_under_the_cap() -> None:
    result = render_pdf(text_template(), named_records(1), max_output_bytes=10_000_000)
    assert result.pdf_bytes


# --- record validation ----------------------------------------------------


@pytest.mark.parametrize(
    "records",
    ["not a list", b"bytes", [7], [None], ["name"], [{"name": "ok"}, 7], [{3: "numeric key"}]],
)
def test_records_that_are_not_field_maps_are_rejected(records: Any) -> None:
    with pytest.raises(RecordError):
        render_pdf(text_template(), records)


def test_records_are_copied_rather_than_mutated() -> None:
    records = [{"name": "Ada"}]
    render_pdf(text_template(), records)
    assert records == [{"name": "Ada"}]


# --- per-element failure attribution --------------------------------------


def test_a_failing_element_is_attributed_to_its_record_and_element() -> None:
    # The old renderer raised out of the whole sheet with no indication of
    # which record or element was at fault.
    doc = template(dict(TEXT_ELEMENT), image_element())
    result = render_pdf(doc, named_records(2), assets=None)
    assert [(w.record_index, w.element_index) for w in result.warnings] == [(0, 1), (1, 1)]
    assert all(w.code == "asset_error" for w in result.warnings)


def test_a_failing_element_does_not_stop_the_other_labels() -> None:
    doc = template(dict(TEXT_ELEMENT), image_element())
    result = render_pdf(doc, named_records(2), assets=None)
    assert result.page_count == 1
    assert "record-0" in text_of(result)
    assert "record-1" in text_of(result)


def test_strict_mode_raises_the_element_failure_instead_of_warning() -> None:
    doc = template(dict(TEXT_ELEMENT), image_element())
    with pytest.raises(AssetError):
        render_pdf(doc, named_records(1), assets=None, options=RenderOptions(strict=True))


def test_strict_mode_raises_a_render_error_carrying_the_failing_indexes() -> None:
    doc = template(dict(TEXT_ELEMENT), barcode_element(field="gtin"))
    records = [{"name": "ok", "gtin": "4006381333931"}, {"name": "bad", "gtin": "4006381333930"}]
    with pytest.raises(RenderError) as caught:
        render_pdf(doc, records, options=RenderOptions(strict=True))
    assert caught.value.record_index == 1
    assert caught.value.element_index == 1
    assert caught.value.to_dict()["record_index"] == 1


def test_warning_dicts_omit_attribution_that_is_not_known() -> None:
    doc = template({**TEXT_ELEMENT, "x_mm": 80.0, "width_mm": 40.0})
    result = render_pdf(doc, named_records(1))
    codes = {warning.code for warning in result.warnings}
    assert "element_exceeds_label" in codes
    geometry_warning = next(w for w in result.warnings if w.code == "element_exceeds_label")
    assert geometry_warning.record_index is None
    assert "record_index" not in geometry_warning.to_dict()


# --- text -----------------------------------------------------------------


def test_a_template_placeholder_is_filled_from_the_record() -> None:
    doc = template({**TEXT_ELEMENT, "field": None, "template": "Ship to {name}"})
    result = render_pdf(doc, [{"name": "Ada Lovelace"}])
    assert "Ship to Ada Lovelace" in text_of(result)


@pytest.mark.parametrize(
    "overrides",
    [{"field": "absent"}, {"field": None, "template": "{absent}"}, {"field": "name"}],
)
def test_a_missing_field_renders_nothing_not_the_word_none(overrides: dict[str, Any]) -> None:
    # resolve_content used to stringify a missing value, printing the literal
    # text "None" onto the label.
    result = render_pdf(template({**TEXT_ELEMENT, **overrides}), [{"other": "x"}])
    assert "None" not in text_of(result)


def test_a_literal_brace_survives_substitution() -> None:
    doc = template({**TEXT_ELEMENT, "field": None, "template": "{{ {name} }}"})
    assert "{ Ada }" in text_of(render_pdf(doc, [{"name": "Ada"}]))


def test_markup_characters_in_a_record_are_drawn_literally() -> None:
    # Paragraph parses its input as mini-XML; unescaped record data either
    # vanished or broke the layout.
    result = render_pdf(text_template(), [{"name": "Ada & <Co>"}])
    assert "Ada & <Co>" in text_of(result)


def test_an_unregistered_font_is_a_validation_error_not_a_key_error() -> None:
    # A bad font name used to surface as a bare KeyError from inside
    # reportlab's metrics tables, which the API turned into a 500.
    with pytest.raises(ValidationError) as caught:
        render_pdf(
            text_template(font_name="Helvitica"),
            named_records(1),
            options=RenderOptions(strict=True),
        )
    assert "Helvetica" in caught.value.message


def test_an_unregistered_font_is_a_warning_in_non_strict_mode() -> None:
    result = render_pdf(text_template(font_name="Comic Sans"), named_records(1))
    assert [w.code for w in result.warnings] == ["validation_error"]


@pytest.mark.parametrize("font_name", ["Helvetica", "Times-Roman", "Courier-Bold"])
def test_the_standard_fonts_render(font_name: str) -> None:
    result = render_pdf(text_template(font_name=font_name), named_records(1))
    assert result.warnings == []


def test_the_error_overflow_policy_reports_the_space_the_text_needs() -> None:
    doc = template(
        {
            **TEXT_ELEMENT,
            "field": None,
            "value": "word " * 200,
            "height_mm": 4.0,
            "overflow": "error",
        }
    )
    with pytest.raises(RenderError) as caught:
        render_pdf(doc, [{}], options=RenderOptions(strict=True))
    assert "overflow" in caught.value.message


def test_the_truncate_policy_marks_the_text_it_dropped() -> None:
    long_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    doc = template(
        {
            **TEXT_ELEMENT,
            "field": None,
            "value": long_text,
            "width_mm": 30.0,
            "height_mm": 6.0,
            "overflow": "truncate",
        }
    )
    drawn = text_of(render_pdf(doc, [{}], options=RenderOptions(strict=True)))
    assert "..." in drawn
    assert "kappa" not in drawn


def test_the_shrink_policy_keeps_the_whole_text() -> None:
    long_text = "alpha beta gamma delta epsilon zeta"
    doc = template(
        {
            **TEXT_ELEMENT,
            "field": None,
            "value": long_text,
            "width_mm": 40.0,
            "height_mm": 8.0,
            "overflow": "shrink",
        }
    )
    drawn = text_of(render_pdf(doc, [{}], options=RenderOptions(strict=True)))
    assert "zeta" in drawn


@pytest.mark.parametrize("rotation", [float("nan"), float("inf")])
def test_a_non_finite_text_rotation_override_is_rejected(rotation: float) -> None:
    with pytest.raises(ValidationError):
        render_pdf(
            text_template(),
            named_records(1),
            options=RenderOptions(text_rotation_deg=rotation, strict=True),
        )


def test_the_short_hex_colour_form_expands_to_full_channels() -> None:
    # HexColor("#abc") parses as 0x000abc, a near-black blue, rather than as
    # "#aabbcc", so the short form had to be expanded before parsing.
    assert parse_color("#abc").hexval() == parse_color("#aabbcc").hexval()


@pytest.mark.parametrize("value", ["red", "", "rgb(1,2,3)", "#ggg"])
def test_a_colour_that_is_not_hex_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_color(value)


@pytest.mark.parametrize(
    ("element_kwargs", "record", "expected"),
    [
        ({"field": "name"}, {"name": "Ada"}, "Ada"),
        ({"field": "name"}, {"name": 42}, "42"),
        ({"field": "name"}, {}, None),
        ({"field": "name"}, {"name": None}, None),
        ({"value": "static"}, {}, "static"),
        ({"template": "{a}-{b}"}, {"a": "x"}, "x-"),
    ],
)
def test_resolve_content_returns_the_string_an_element_draws(
    element_kwargs: dict[str, Any], record: dict[str, Any], expected: str | None
) -> None:
    element = TextElement(x_mm=0, y_mm=0, width_mm=10, height_mm=10, **element_kwargs)
    assert resolve_content(element, record) == expected


def test_element_coordinates_are_measured_from_the_top_left_of_the_label() -> None:
    element = TextElement(x_mm=10, y_mm=10, width_mm=20, height_mm=5)
    x_pt, y_pt, width_pt, height_pt = element_box_pt(slot(), element)
    assert x_pt == pytest.approx(mm_to_pt(10))
    assert width_pt == pytest.approx(mm_to_pt(20))
    assert height_pt == pytest.approx(mm_to_pt(5))
    # PDF y grows upward, so a 10mm top offset sits near the top of the slot.
    assert y_pt == pytest.approx(100.0 - mm_to_pt(10) - mm_to_pt(5))


def test_an_element_box_outside_its_label_has_no_drawable_area() -> None:
    element = TextElement(x_mm=500, y_mm=0)
    with pytest.raises(GeometryError):
        element_box_pt(slot(), element)


# --- barcodes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("978030640615", "7"),
        ("400638133393", "1"),
        ("001234567890", "5"),
        ("501234567890", "0"),
        ("000000000000", "0"),
    ],
)
def test_the_ean13_check_digit_matches_known_gtins(body: str, expected: str) -> None:
    assert ean13_check_digit(body) == expected


@pytest.mark.parametrize("body", ["", "12345", "40063813339", "4006381333931", "40063813339x"])
def test_the_check_digit_helper_needs_exactly_twelve_digits(body: str) -> None:
    with pytest.raises(RenderError):
        ean13_check_digit(body)


def test_a_twelve_digit_ean13_payload_is_completed_for_the_caller() -> None:
    doc = template(barcode_element(value="400638133393"))
    result = render_pdf(doc, [{}], options=RenderOptions(strict=True))
    assert result.warnings == []


def test_a_thirteen_digit_ean13_payload_with_a_good_check_digit_is_accepted() -> None:
    doc = template(barcode_element(value="4006381333931"))
    assert render_pdf(doc, [{}], options=RenderOptions(strict=True)).warnings == []


def test_a_wrong_ean13_check_digit_is_refused_and_names_the_right_one() -> None:
    # The old code sliced a 13-digit payload to digits[:12] and let reportlab
    # recompute the check digit, so a mistyped digit silently produced a
    # different, perfectly scannable barcode for a different product.
    doc = template(barcode_element(value="4006381333930"))
    with pytest.raises(RenderError) as caught:
        render_pdf(doc, [{}], options=RenderOptions(strict=True))
    assert "400638133393" in caught.value.message
    assert "1" in caught.value.message


@pytest.mark.parametrize("payload", ["12345", "1234567890", "12345678901234", "4006381333931x"])
def test_an_ean13_payload_of_the_wrong_shape_is_refused(payload: str) -> None:
    doc = template(barcode_element(value=payload))
    with pytest.raises(RenderError):
        render_pdf(doc, [{}], options=RenderOptions(strict=True))


def test_separators_in_a_printed_gtin_are_ignored() -> None:
    doc = template(barcode_element(value="400 6381-333931"))
    assert render_pdf(doc, [{}], options=RenderOptions(strict=True)).warnings == []


@pytest.mark.parametrize(
    ("barcode_type", "value"),
    [("code128", "SKU-0001"), ("qr", "https://example.com/label"), ("ean13", "4006381333931")],
)
def test_each_supported_symbology_renders(barcode_type: str, value: str) -> None:
    doc = template(barcode_element(barcode_type=barcode_type, value=value))
    assert render_pdf(doc, [{}], options=RenderOptions(strict=True)).warnings == []


def test_a_payload_code128_cannot_encode_is_reported_as_a_render_error() -> None:
    doc = template(barcode_element(barcode_type="code128", value="中文"))
    with pytest.raises(RenderError) as caught:
        render_pdf(doc, [{}], options=RenderOptions(strict=True))
    assert "code128" in caught.value.message


@pytest.mark.parametrize(
    ("barcode_type", "value"),
    [("code128", "ABCDEFGHIJKLMNOPQRST"), ("qr", "https://example.com/a/very/long/url/here")],
)
def test_a_barcode_too_small_to_scan_is_refused(barcode_type: str, value: str) -> None:
    # Emitting an unscannable symbol is worse than emitting none: it looks
    # right and cannot be read.
    doc = template(
        barcode_element(barcode_type=barcode_type, value=value, width_mm=5.0, height_mm=4.0)
    )
    with pytest.raises(RenderError) as caught:
        render_pdf(doc, [{}], options=RenderOptions(strict=True))
    assert "scannable" in caught.value.message


def test_a_barcode_with_no_content_draws_nothing_rather_than_failing() -> None:
    doc = template(barcode_element(field="absent"))
    assert render_pdf(doc, [{}], options=RenderOptions(strict=True)).warnings == []


# --- images ---------------------------------------------------------------


def test_an_image_is_embedded_when_an_asset_root_is_configured(loader: AssetLoader) -> None:
    result = render_pdf(
        template(image_element()), [{}], assets=loader, options=RenderOptions(strict=True)
    )
    assert result.warnings == []
    assert pages(result)[0]["/Resources"].get("/XObject")


def test_a_page_without_images_embeds_no_xobject() -> None:
    result = render_pdf(text_template(), named_records(1))
    assert not pages(result)[0]["/Resources"].get("/XObject")


@pytest.mark.parametrize("fit", ["contain", "cover", "stretch"])
def test_every_image_fit_policy_renders(loader: AssetLoader, fit: str) -> None:
    doc = template(image_element(fit=fit, width_mm=20.0, height_mm=30.0))
    assert render_pdf(doc, [{}], assets=loader, options=RenderOptions(strict=True)).warnings == []


def test_images_are_disabled_when_no_asset_loader_is_supplied() -> None:
    with pytest.raises(AssetError) as caught:
        render_pdf(template(image_element()), [{}], assets=None, options=RenderOptions(strict=True))
    assert "asset" in caught.value.message


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("missing.png", "not_found"),
        ("../secret.png", "unsafe_path"),
        ("/etc/passwd", "asset_error"),
        ("logo.txt", "asset_error"),
    ],
)
def test_an_image_reference_outside_the_asset_root_is_refused(
    loader: AssetLoader, reference: str, code: str
) -> None:
    # A reference is record data. The old code called Path(raw) directly and
    # honoured absolute and relative paths, so a record could read any file.
    result = render_pdf(template(image_element(value=reference)), [{}], assets=loader)
    assert [warning.code for warning in result.warnings] == [code]


def test_an_image_element_with_no_reference_draws_nothing(loader: AssetLoader) -> None:
    doc = template(image_element(value=None, field="absent"))
    assert render_pdf(doc, [{}], assets=loader, options=RenderOptions(strict=True)).warnings == []


# --- PNG preview ----------------------------------------------------------


def test_render_page_png_returns_png_bytes() -> None:
    result = render_pdf(text_template(), named_records(1))
    assert render_page_png(result.pdf_bytes, page=0, scale=0.5).startswith(PNG_MAGIC)


def test_a_page_past_the_end_is_clamped_to_the_last_page() -> None:
    result = render_pdf(text_template(), named_records(CELLS_PER_PAGE + 1))
    last = render_page_png(result.pdf_bytes, page=1, scale=0.3)
    assert render_page_png(result.pdf_bytes, page=999, scale=0.3) == last


def test_each_page_of_a_document_previews_differently() -> None:
    result = render_pdf(text_template(), named_records(CELLS_PER_PAGE + 1))
    first = render_page_png(result.pdf_bytes, page=0, scale=0.3)
    assert first != render_page_png(result.pdf_bytes, page=1, scale=0.3)


def test_a_negative_page_is_rejected() -> None:
    result = render_pdf(text_template(), named_records(1))
    with pytest.raises(ValidationError) as caught:
        render_page_png(result.pdf_bytes, page=-1)
    assert caught.value.loc == ("page",)


@pytest.mark.parametrize("scale", [0.0, -1.0, 99.0, math.nan, math.inf])
def test_a_scale_outside_the_supported_range_is_rejected(scale: float) -> None:
    result = render_pdf(text_template(), named_records(1))
    with pytest.raises(ValidationError) as caught:
        render_page_png(result.pdf_bytes, scale=scale)
    assert caught.value.loc == ("scale",)


def test_a_scale_that_would_blow_the_pixel_cap_is_refused() -> None:
    result = render_pdf(text_template(), named_records(1))
    with pytest.raises(LimitExceeded) as caught:
        render_page_png(result.pdf_bytes, scale=10.0)
    assert caught.value.limit_name == "max_pixels"
    assert caught.value.actual is not None
    assert caught.value.actual > caught.value.limit


def test_a_pixel_cap_below_one_is_rejected() -> None:
    result = render_pdf(text_template(), named_records(1))
    with pytest.raises(ValidationError):
        render_page_png(result.pdf_bytes, max_pixels=0)


@pytest.mark.parametrize("payload", [b"", b"not a pdf at all", "a string", None])
def test_bytes_that_are_not_a_pdf_are_rejected(payload: Any) -> None:
    with pytest.raises(ValidationError):
        render_page_png(payload)
    with pytest.raises(ValidationError):
        page_count(payload)
