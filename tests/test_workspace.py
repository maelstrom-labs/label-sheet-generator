import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from label_sheet_generator.avery import build_template_from_avery_preset
from label_sheet_generator.io import save_template, save_text_layout_template
from label_sheet_generator.models import BarcodeElement, TextElement, TextLayoutTemplate
from label_sheet_generator.workspace import (
    apply_layout_template,
    apply_margin_overrides,
    analyze_record_alignment,
    build_records_from_field_inputs,
    build_record_document,
    convert_import_data_to_json,
    extract_field_names,
    get_workspace_config,
    parse_csv_records,
    parse_record_document,
    prepare_render_state,
    render_pdf_bytes,
    render_preview_image_bytes,
)


def test_extract_field_names_prioritizes_name_and_template_fields() -> None:
    fields = extract_field_names(
        [
            TextElement(field="sku", x_mm=1, y_mm=1, width_mm=10, height_mm=4),
            TextElement(template="{address_1}\n{address_2}", x_mm=1, y_mm=1, width_mm=10, height_mm=4),
            BarcodeElement(field="name", x_mm=1, y_mm=1, width_mm=10, height_mm=4),
        ]
    )

    assert fields == ["name", "sku", "address_1", "address_2"]


def test_build_records_from_field_inputs_merges_columns() -> None:
    records = build_records_from_field_inputs(
        {
            "name": "Ada Lovelace\nGrace Hopper",
            "address_1": "12 Analytical Engine Way",
            "sku": "AL-1001\nGH-2002",
        }
    )

    assert records == [
        {
            "name": "Ada Lovelace",
            "address_1": "12 Analytical Engine Way",
            "sku": "AL-1001",
        },
        {
            "name": "Grace Hopper",
            "address_1": "",
            "sku": "GH-2002",
        },
    ]


def test_build_record_document_seeds_schema_with_empty_records() -> None:
    document = build_record_document(["name", "address_1", "address_2"])

    assert document == {
        "schema": ["name", "address_1", "address_2"],
        "records": [],
    }


def test_analyze_record_alignment_reports_missing_and_extra_fields() -> None:
    available_schema, missing_fields, extra_fields = analyze_record_alignment(
        ["name", "sku"],
        declared_schema=["name", "description"],
        records=[{"name": "Ada Lovelace", "description": "Pioneer"}],
    )

    assert available_schema == ["name", "description"]
    assert missing_fields == ["sku"]
    assert extra_fields == ["description"]


def test_parse_record_document_accepts_records_wrapper() -> None:
    declared_schema, records = parse_record_document(
        json.dumps(
            {
                "schema": ["name", "sku"],
                "records": [{"name": "Ada Lovelace", "sku": "AL-1001"}],
            }
        )
    )

    assert declared_schema == ["name", "sku"]
    assert records == [{"name": "Ada Lovelace", "sku": "AL-1001"}]


def test_parse_csv_records_uses_header_row_and_skips_blank_lines() -> None:
    records = parse_csv_records("name,sku\nAda Lovelace,AL-1001\n\nGrace Hopper,GH-2002\n")

    assert records == [
        {"name": "Ada Lovelace", "sku": "AL-1001"},
        {"name": "Grace Hopper", "sku": "GH-2002"},
    ]


def test_convert_import_data_to_json_converts_csv_into_json_document() -> None:
    payload = convert_import_data_to_json(
        "records.csv",
        b"name,category\nGARLIC,SEASONING\nBAY LEAVES,HERB\n",
        fields=["name", "category", "descriptor"],
    )

    assert json.loads(payload) == {
        "schema": ["name", "category", "descriptor"],
        "records": [
            {"name": "GARLIC", "category": "SEASONING"},
            {"name": "BAY LEAVES", "category": "HERB"},
        ],
    }


def test_apply_layout_template_replaces_elements_and_sets_metadata() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")
    layout_template = TextLayoutTemplate(
        name="names-only",
        elements=[TextElement(field="name", x_mm=4, y_mm=3, width_mm=10, height_mm=4)],
    )

    composed_template = apply_layout_template(
        label_template,
        layout_template,
        layout_path=Path("templates/layouts/names-only.json"),
    )

    assert composed_template.page == label_template.page
    assert composed_template.grid == label_template.grid
    assert len(composed_template.elements) == 1
    assert composed_template.elements[0].field == "name"
    assert composed_template.metadata["layout_template"] == "names-only"


def test_apply_margin_overrides_clones_grid_with_new_values() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")

    overridden_template = apply_margin_overrides(
        label_template,
        margin_top_mm=14.0,
        margin_right_mm=6.5,
        margin_bottom_mm=10.0,
        margin_left_mm=8.0,
    )

    assert overridden_template.grid.margin_top_mm == 14.0
    assert overridden_template.grid.margin_right_mm == 6.5
    assert overridden_template.grid.margin_bottom_mm == 10.0
    assert overridden_template.grid.margin_left_mm == 8.0
    assert label_template.grid.margin_top_mm != overridden_template.grid.margin_top_mm


def test_get_workspace_config_returns_seeded_document_and_margins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "labels" / "sheet.json",
        build_template_from_avery_preset("5163", name="sheet"),
    )
    save_text_layout_template(
        tmp_path / "templates" / "layouts" / "address-layout.json",
        TextLayoutTemplate(
            name="address-layout",
            elements=[
                TextElement(field="name", x_mm=4, y_mm=3, width_mm=58, height_mm=8),
                TextElement(template="{address_1}", x_mm=4, y_mm=11, width_mm=58, height_mm=8),
            ],
        ),
    )

    config = get_workspace_config("labels/sheet", "layouts/address-layout")

    assert config.label_template_name == "sheet"
    assert config.layout_name == "address-layout"
    assert config.fields == ["name", "address_1"]
    assert json.loads(config.document) == {"schema": ["name", "address_1"], "records": []}
    assert config.margin_top_mm == pytest.approx(build_template_from_avery_preset("5163").grid.margin_top_mm)


def test_prepare_render_state_builds_preview_records(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "labels" / "sheet.json",
        build_template_from_avery_preset("5163", name="sheet"),
    )

    config = get_workspace_config("labels/sheet")
    state = prepare_render_state(
        config,
        data_document=json.dumps({
            "schema": ["name"],
            "records": [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}],
        }),
        margin_top_mm=config.margin_top_mm,
        margin_right_mm=config.margin_right_mm,
        margin_bottom_mm=config.margin_bottom_mm,
        margin_left_mm=config.margin_left_mm,
    )

    assert state.record_document_error is None
    assert state.full_records == [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}]
    assert state.page_count == 1


def test_render_preview_image_bytes_returns_png() -> None:
    pytest.importorskip("pypdfium2")

    label_template = build_template_from_avery_preset("5163", name="shipping")
    pdf_bytes = render_pdf_bytes(
        label_template,
        [{}],
        template_path=Path("templates/labels/shipping.json"),
    )

    preview_image_bytes = render_preview_image_bytes(pdf_bytes)

    assert preview_image_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_pdf_bytes_supports_landscape_orientation() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")
    pdf_bytes = render_pdf_bytes(
        label_template,
        [{}],
        template_path=Path("templates/labels/shipping.json"),
        page_orientation="landscape",
    )

    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]

    assert float(page.mediabox.width) > float(page.mediabox.height)


def test_render_pdf_bytes_supports_page_rotation() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")
    pdf_bytes = render_pdf_bytes(
        label_template,
        [{}],
        template_path=Path("templates/labels/shipping.json"),
        page_orientation="landscape",
        page_rotation_deg=180,
    )

    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]

    assert page.rotation == 180


def test_render_pdf_bytes_supports_text_rotation_override() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")
    label_template.elements = [
        TextElement(
            field="name",
            x_mm=4,
            y_mm=3,
            width_mm=58,
            height_mm=8,
            font_size_pt=11,
        )
    ]

    baseline_pdf_bytes = render_pdf_bytes(
        label_template,
        [{"name": "Ada Lovelace"}],
        template_path=Path("templates/labels/shipping.json"),
    )
    rotated_pdf_bytes = render_pdf_bytes(
        label_template,
        [{"name": "Ada Lovelace"}],
        template_path=Path("templates/labels/shipping.json"),
        text_rotation_deg=90,
    )

    baseline_content = PdfReader(io.BytesIO(baseline_pdf_bytes)).pages[0].get_contents().get_data()
    rotated_content = PdfReader(io.BytesIO(rotated_pdf_bytes)).pages[0].get_contents().get_data()

    assert baseline_content != rotated_content


def test_render_pdf_bytes_supports_bleed_guides() -> None:
    label_template = build_template_from_avery_preset("5163", name="shipping")

    baseline_pdf_bytes = render_pdf_bytes(
        label_template,
        [{}],
        template_path=Path("templates/labels/shipping.json"),
    )
    guided_pdf_bytes = render_pdf_bytes(
        label_template,
        [{}],
        template_path=Path("templates/labels/shipping.json"),
        bleed_guide_inset_mm=1.5,
    )

    baseline_content = PdfReader(io.BytesIO(baseline_pdf_bytes)).pages[0].get_contents().get_data()
    guided_content = PdfReader(io.BytesIO(guided_pdf_bytes)).pages[0].get_contents().get_data()

    assert baseline_content != guided_content