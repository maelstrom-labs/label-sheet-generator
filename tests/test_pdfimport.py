"""PDF template import: detection accuracy, resource guards, and refusals.

The old importer had no size, page-count, object-count, encryption or time
guard, and would happily return a confidently wrong grid. Both halves are
tested here: that a real sheet round-trips exactly, and that everything else
is refused rather than guessed at.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("pypdf")

from label_sheet_generator.avery import build_template, get_preset
from label_sheet_generator.errors import (
    LabelSheetError,
    LimitExceeded,
    ValidationError,
)
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.pdfimport import (
    LOW_CONFIDENCE_THRESHOLD,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    detect_grid,
    import_template,
    read_page_size_mm,
)
from label_sheet_generator.render import RenderOptions, render_pdf
from label_sheet_generator.schema import LabelTemplate


def sheet_pdf(code: str = "5160", *, records: int = 0) -> bytes:
    """A rendered label sheet with visible slot outlines, as a PDF to import."""
    template = build_template(code)
    rows = [{} for _ in range(records)]
    return render_pdf(template, rows, options=RenderOptions(outline_slots=True)).pdf_bytes


# --- Detection accuracy ---------------------------------------------------


@pytest.mark.parametrize("code", ["5160", "5161", "5162", "5163", "5164"])
def test_a_rendered_sheet_round_trips_to_its_own_geometry(code: str) -> None:
    preset = get_preset(code)
    report = import_template(sheet_pdf(code))

    assert report.detected is not None, f"{code} was not detected"
    grid = LabelTemplate.model_validate(report.template.dump()).grid
    assert grid.rows == preset.rows
    assert grid.cols == preset.cols
    assert grid.label_width_mm == pytest.approx(preset.label_width_mm, abs=0.05)
    assert grid.label_height_mm == pytest.approx(preset.label_height_mm, abs=0.05)
    assert grid.margin_left_mm == pytest.approx(preset.margin_left_mm, abs=0.05)
    assert grid.margin_top_mm == pytest.approx(preset.margin_top_mm, abs=0.05)
    assert grid.gap_x_mm == pytest.approx(preset.gap_x_mm, abs=0.05)


def test_the_imported_template_is_geometrically_sound() -> None:
    report = import_template(sheet_pdf("5160"))
    assert validate_geometry(report.template).ok


def test_page_size_is_read_correctly() -> None:
    width_mm, height_mm = read_page_size_mm(sheet_pdf("5160"))
    assert width_mm == pytest.approx(215.9, abs=0.1)
    assert height_mm == pytest.approx(279.4, abs=0.1)


def test_a_clean_vector_sheet_reports_high_confidence() -> None:
    detected = detect_grid(sheet_pdf("5160"))
    assert detected is not None
    assert detected.method == "rectangles"
    assert detected.confidence > LOW_CONFIDENCE_THRESHOLD


def test_metadata_records_provenance_without_a_filesystem_path() -> None:
    report = import_template(sheet_pdf("5160"), name="my-sheet")
    metadata = report.template.metadata
    assert metadata["import_source_format"] == "pdf"
    assert metadata["auto_detected"] is True
    assert metadata["auto_detected_method"] == "rectangles"
    assert 0.0 <= metadata["auto_detected_confidence"] <= 1.0
    # A path here would leak the importing machine's layout into a template
    # that is meant to be shared.
    assert "/" not in str(metadata.get("imported_from", ""))


# --- Overrides ------------------------------------------------------------


def test_an_explicit_value_beats_detection() -> None:
    report = import_template(sheet_pdf("5160"), rows=4, cols=2)
    assert report.template.grid.rows == 4
    assert report.template.grid.cols == 2


def test_an_explicit_zero_is_honoured_not_treated_as_missing() -> None:
    # The old code resolved values with `explicit or detected or preset`, so a
    # legitimate 0 silently fell through to the next source.
    report = import_template(sheet_pdf("5160"), gap_x_mm=0.0)
    assert report.template.grid.gap_x_mm == 0.0


def test_a_preset_supplies_what_detection_and_flags_do_not() -> None:
    preset = get_preset("5160")
    report = import_template(b"%PDF-1.4\n" + sheet_pdf("5160")[9:], preset_code="5160")
    assert report.template.grid.rows == preset.rows


def test_margins_are_recomputed_from_the_page_and_never_negative() -> None:
    grid = import_template(sheet_pdf("5160")).template.grid
    assert grid.margin_right_mm >= 0
    assert grid.margin_bottom_mm >= 0


# --- Resource guards ------------------------------------------------------


def test_a_non_pdf_is_refused_by_its_header() -> None:
    with pytest.raises(ValidationError, match="%PDF"):
        import_template(b"<html><body>not a pdf</body></html>")


def test_an_empty_buffer_is_refused() -> None:
    with pytest.raises(ValidationError, match="empty"):
        import_template(b"")


def test_a_truncated_pdf_produces_a_typed_error_not_a_parser_traceback() -> None:
    with pytest.raises(LabelSheetError):
        import_template(sheet_pdf("5160")[:400])


def test_an_oversized_buffer_is_refused_before_parsing() -> None:
    with pytest.raises(LimitExceeded) as excinfo:
        import_template(b"%PDF-1.4\n" + b"\x00" * (MAX_PDF_BYTES + 1))
    assert excinfo.value.limit_name == "max_pdf_bytes"
    assert excinfo.value.limit == MAX_PDF_BYTES


def test_too_many_pages_is_refused_rather_than_silently_ignored() -> None:
    template = build_template("5160")
    many = render_pdf(
        template, [{} for _ in range((MAX_PDF_PAGES + 2) * template.grid.cells_per_page)]
    ).pdf_bytes
    with pytest.raises(LimitExceeded) as excinfo:
        import_template(many)
    assert excinfo.value.limit_name == "max_pdf_pages"


@pytest.mark.parametrize(
    "payload",
    [b"not a pdf", b"", b"%PDF", b"%PDF-1.4\ngarbage", b"\x00\x01\x02\x03"],
)
def test_no_hostile_input_escapes_as_an_untyped_exception(payload: bytes) -> None:
    # The importer is reachable from an upload when the feature is enabled, so
    # an untyped exception here would become an HTTP 500.
    with pytest.raises(LabelSheetError):
        import_template(payload)


def test_a_non_bytes_argument_is_rejected_clearly() -> None:
    with pytest.raises((ValidationError, TypeError)):
        import_template("/tmp/some.pdf")  # type: ignore[arg-type]


# --- Refusing a confidently wrong answer ----------------------------------


def test_a_blank_page_detects_nothing_rather_than_inventing_a_grid() -> None:
    blank = LabelTemplate.model_validate(
        {"page": {"width_mm": 215.9, "height_mm": 279.4}, "grid": {"rows": 1, "cols": 1}}
    )
    assert detect_grid(render_pdf(blank, []).pdf_bytes) is None


def test_an_undetectable_page_names_the_flags_it_needs() -> None:
    blank = LabelTemplate.model_validate(
        {"page": {"width_mm": 215.9, "height_mm": 279.4}, "grid": {"rows": 1, "cols": 1}}
    )
    with pytest.raises(LabelSheetError) as excinfo:
        import_template(render_pdf(blank, []).pdf_bytes)
    message = str(excinfo.value)
    assert "--rows" in message
    assert "--cols" in message


def test_a_low_confidence_detection_is_flagged_for_the_user_to_check() -> None:
    report = import_template(sheet_pdf("5160"))
    if report.detected and report.detected.confidence < LOW_CONFIDENCE_THRESHOLD:
        assert any("check" in warning.lower() for warning in report.warnings)


def test_detected_grid_serialises_for_the_api() -> None:
    detected = detect_grid(sheet_pdf("5160"))
    assert detected is not None
    payload = detected.to_dict()
    assert payload["rows"] == 10
    assert payload["cols"] == 3
    assert payload["method"] == "rectangles"
    assert 0.0 <= payload["confidence"] <= 1.0


def test_import_report_serialises_for_the_api() -> None:
    payload = import_template(sheet_pdf("5160")).to_dict()
    assert "template" in payload
    assert "page_size_mm" in payload
    assert isinstance(payload["warnings"], list)
