from pathlib import Path

import pytest
from pypdf import PdfReader

from label_sheet_generator.models import GridSpec, LabelTemplate, PageSpec, TemplateError, TextElement
from label_sheet_generator.rendering import compute_slots, render_pdf
from label_sheet_generator.units import mm_to_pt


def test_compute_slots_returns_row_major_positions() -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=100, height_mm=80),
        grid=GridSpec(
            rows=2,
            cols=2,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            gap_x_mm=5,
            gap_y_mm=5,
            label_width_mm=42.5,
            label_height_mm=32.5,
        ),
    )

    slots = compute_slots(template)

    assert len(slots) == 4
    assert slots[0].row == 0
    assert slots[0].col == 0
    assert slots[1].row == 0
    assert slots[1].col == 1
    assert slots[2].row == 1
    assert slots[2].col == 0


def test_render_pdf_paginates_records(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=100, height_mm=80),
        grid=GridSpec(
            rows=2,
            cols=2,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            gap_x_mm=5,
            gap_y_mm=5,
            label_width_mm=42.5,
            label_height_mm=32.5,
        ),
        elements=[
            TextElement(
                field="name",
                x_mm=3,
                y_mm=3,
                width_mm=36,
                height_mm=10,
                font_size_pt=10,
            )
        ],
    )
    records = [{"name": f"Label {index}"} for index in range(5)]
    output_path = tmp_path / "labels.pdf"

    render_pdf(template, records, output_path, template_path=tmp_path / "template.json")

    reader = PdfReader(str(output_path))
    assert len(reader.pages) == 2


def test_render_pdf_supports_landscape_orientation(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=80, height_mm=100),
        grid=GridSpec(
            rows=2,
            cols=2,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            gap_x_mm=5,
            gap_y_mm=5,
            label_width_mm=32.5,
            label_height_mm=42.5,
        ),
        elements=[
            TextElement(
                field="name",
                x_mm=3,
                y_mm=3,
                width_mm=26,
                height_mm=10,
                font_size_pt=10,
            )
        ],
    )
    output_path = tmp_path / "labels-landscape.pdf"

    render_pdf(
        template,
        [{"name": "Label 1"}],
        output_path,
        template_path=tmp_path / "template.json",
        page_orientation="landscape",
    )

    reader = PdfReader(str(output_path))
    page = reader.pages[0]
    assert float(page.mediabox.width) == pytest.approx(mm_to_pt(100))
    assert float(page.mediabox.height) == pytest.approx(mm_to_pt(80))


def test_render_pdf_supports_page_rotation(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=80, height_mm=100),
        grid=GridSpec(
            rows=1,
            cols=1,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            label_width_mm=70,
            label_height_mm=90,
        ),
        elements=[
            TextElement(
                field="name",
                x_mm=3,
                y_mm=3,
                width_mm=60,
                height_mm=10,
                font_size_pt=10,
            )
        ],
    )
    output_path = tmp_path / "labels-rotated.pdf"

    render_pdf(
        template,
        [{"name": "Label 1"}],
        output_path,
        template_path=tmp_path / "template.json",
        page_orientation="landscape",
        page_rotation_deg=180,
    )

    reader = PdfReader(str(output_path))
    page = reader.pages[0]
    assert float(page.mediabox.width) == pytest.approx(mm_to_pt(100))
    assert float(page.mediabox.height) == pytest.approx(mm_to_pt(80))
    assert page.rotation == 180


def test_render_pdf_rejects_unknown_page_orientation(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=80, height_mm=100),
        grid=GridSpec(
            rows=1,
            cols=1,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            label_width_mm=70,
            label_height_mm=90,
        ),
    )

    with pytest.raises(TemplateError, match="page_orientation must be 'portrait' or 'landscape'"):
        render_pdf(
            template,
            [{}],
            tmp_path / "labels.pdf",
            template_path=tmp_path / "template.json",
            page_orientation="sideways",
        )


def test_render_pdf_rejects_unknown_page_rotation(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=80, height_mm=100),
        grid=GridSpec(
            rows=1,
            cols=1,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            label_width_mm=70,
            label_height_mm=90,
        ),
    )

    with pytest.raises(TemplateError, match="page_rotation_deg must be 0, 90, 180, or 270"):
        render_pdf(
            template,
            [{}],
            tmp_path / "labels.pdf",
            template_path=tmp_path / "template.json",
            page_rotation_deg=45,
        )


def test_render_pdf_changes_content_for_text_rotation_override(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=100, height_mm=80),
        grid=GridSpec(
            rows=1,
            cols=1,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            label_width_mm=90,
            label_height_mm=70,
        ),
        elements=[
            TextElement(
                field="name",
                x_mm=5,
                y_mm=5,
                width_mm=80,
                height_mm=20,
                font_size_pt=12,
            )
        ],
    )
    baseline_path = tmp_path / "baseline.pdf"
    rotated_path = tmp_path / "rotated.pdf"

    render_pdf(
        template,
        [{"name": "Label 1"}],
        baseline_path,
        template_path=tmp_path / "template.json",
    )
    render_pdf(
        template,
        [{"name": "Label 1"}],
        rotated_path,
        template_path=tmp_path / "template.json",
        text_rotation_deg=90,
    )

    baseline_content = PdfReader(str(baseline_path)).pages[0].get_contents().get_data()
    rotated_content = PdfReader(str(rotated_path)).pages[0].get_contents().get_data()

    assert baseline_content != rotated_content


def test_render_pdf_changes_content_for_bleed_guides(tmp_path: Path) -> None:
    template = LabelTemplate(
        page=PageSpec(width_mm=100, height_mm=80),
        grid=GridSpec(
            rows=1,
            cols=1,
            margin_left_mm=5,
            margin_top_mm=5,
            margin_right_mm=5,
            margin_bottom_mm=5,
            label_width_mm=90,
            label_height_mm=70,
        ),
    )
    baseline_path = tmp_path / "baseline.pdf"
    guided_path = tmp_path / "guided.pdf"

    render_pdf(
        template,
        [{}],
        baseline_path,
        template_path=tmp_path / "template.json",
    )
    render_pdf(
        template,
        [{}],
        guided_path,
        template_path=tmp_path / "template.json",
        bleed_guide_inset_mm=1.5,
    )

    baseline_content = PdfReader(str(baseline_path)).pages[0].get_contents().get_data()
    guided_content = PdfReader(str(guided_path)).pages[0].get_contents().get_data()

    assert baseline_content != guided_content
