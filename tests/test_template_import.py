from pathlib import Path

from reportlab.pdfgen import canvas

from label_sheet_generator.template_import import detect_grid_from_pdf, import_template_from_pdf
from label_sheet_generator.units import mm_to_pt


def _build_pdf_template(path: Path) -> None:
    page_width_mm = 110
    page_height_mm = 90
    margin_left_mm = 5
    margin_top_mm = 10
    label_width_mm = 30
    label_height_mm = 20
    gap_x_mm = 4
    gap_y_mm = 6

    page_width_pt = mm_to_pt(page_width_mm)
    page_height_pt = mm_to_pt(page_height_mm)
    pdf = canvas.Canvas(str(path), pagesize=(page_width_pt, page_height_pt))

    for row in range(2):
        top_offset_pt = mm_to_pt(margin_top_mm + row * (label_height_mm + gap_y_mm))
        y_pt = page_height_pt - top_offset_pt - mm_to_pt(label_height_mm)
        for col in range(3):
            x_pt = mm_to_pt(margin_left_mm + col * (label_width_mm + gap_x_mm))
            pdf.rect(x_pt, y_pt, mm_to_pt(label_width_mm), mm_to_pt(label_height_mm), stroke=1, fill=0)

    pdf.save()


def _build_line_only_pdf_template(path: Path) -> None:
    page_width_mm = 110
    page_height_mm = 90
    margin_left_mm = 5
    margin_top_mm = 10
    label_width_mm = 30
    label_height_mm = 20
    gap_x_mm = 4
    gap_y_mm = 6

    page_width_pt = mm_to_pt(page_width_mm)
    page_height_pt = mm_to_pt(page_height_mm)
    pdf = canvas.Canvas(str(path), pagesize=(page_width_pt, page_height_pt))

    for row in range(2):
        top_offset_pt = mm_to_pt(margin_top_mm + row * (label_height_mm + gap_y_mm))
        top_y_pt = page_height_pt - top_offset_pt
        bottom_y_pt = top_y_pt - mm_to_pt(label_height_mm)
        for col in range(3):
            left_x_pt = mm_to_pt(margin_left_mm + col * (label_width_mm + gap_x_mm))
            right_x_pt = left_x_pt + mm_to_pt(label_width_mm)
            pdf.line(left_x_pt, top_y_pt, right_x_pt, top_y_pt)
            pdf.line(left_x_pt, bottom_y_pt, right_x_pt, bottom_y_pt)
            pdf.line(left_x_pt, bottom_y_pt, left_x_pt, top_y_pt)
            pdf.line(right_x_pt, bottom_y_pt, right_x_pt, top_y_pt)

    pdf.save()


def _build_curve_pdf_template(path: Path) -> None:
    page_width_mm = 110
    page_height_mm = 90
    margin_left_mm = 5
    margin_top_mm = 10
    label_width_mm = 30
    label_height_mm = 20
    gap_x_mm = 4
    gap_y_mm = 6

    page_width_pt = mm_to_pt(page_width_mm)
    page_height_pt = mm_to_pt(page_height_mm)
    pdf = canvas.Canvas(str(path), pagesize=(page_width_pt, page_height_pt))

    for row in range(2):
        top_offset_pt = mm_to_pt(margin_top_mm + row * (label_height_mm + gap_y_mm))
        y_pt = page_height_pt - top_offset_pt - mm_to_pt(label_height_mm)
        for col in range(3):
            x_pt = mm_to_pt(margin_left_mm + col * (label_width_mm + gap_x_mm))
            pdf.roundRect(
                x_pt,
                y_pt,
                mm_to_pt(label_width_mm),
                mm_to_pt(label_height_mm),
                radius=6,
                stroke=1,
                fill=0,
            )

    pdf.save()


def test_detect_grid_from_pdf(tmp_path: Path) -> None:
    source_path = tmp_path / "template.pdf"
    _build_pdf_template(source_path)

    detected = detect_grid_from_pdf(source_path)

    assert detected is not None
    assert detected.rows == 2
    assert detected.cols == 3
    assert abs(detected.label_width_mm - 30.0) < 0.1
    assert abs(detected.label_height_mm - 20.0) < 0.1


def test_import_template_from_pdf(tmp_path: Path) -> None:
    source_path = tmp_path / "template.pdf"
    _build_pdf_template(source_path)

    template = import_template_from_pdf(source_path)

    assert template.grid.rows == 2
    assert template.grid.cols == 3
    assert template.metadata["import_source_format"] == "pdf"


def test_import_template_from_line_only_pdf(tmp_path: Path) -> None:
    source_path = tmp_path / "line-template.pdf"
    _build_line_only_pdf_template(source_path)

    template = import_template_from_pdf(source_path)

    assert template.grid.rows == 2
    assert template.grid.cols == 3
    assert abs(template.grid.label_width_mm - 30.0) < 0.1
    assert abs(template.grid.label_height_mm - 20.0) < 0.1
    assert template.metadata["auto_detected_method"] == "lines"


def test_import_template_from_curve_pdf(tmp_path: Path) -> None:
    source_path = tmp_path / "curve-template.pdf"
    _build_curve_pdf_template(source_path)

    template = import_template_from_pdf(source_path)

    assert template.grid.rows == 2
    assert template.grid.cols == 3
    assert abs(template.grid.label_width_mm - 30.0) < 0.1
    assert abs(template.grid.label_height_mm - 20.0) < 0.1
    assert template.metadata["auto_detected_method"] == "curves"
