from pathlib import Path

from label_sheet_generator.io import (
    load_records,
    load_template,
    load_text_layout_template,
    save_template,
    save_text_layout_template,
)
from label_sheet_generator.models import GridSpec, LabelTemplate, PageSpec, TextElement, TextLayoutTemplate
from label_sheet_generator.units import in_to_mm


def test_load_records_supports_json(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    records_path.write_text(
        "[\n"
        "  {\n"
        "    \"name\": \"Ada Lovelace\",\n"
        "    \"address_1\": \"12 Analytical Engine Way\",\n"
        "    \"address_2\": \"London\",\n"
        "    \"sku\": \"AL-1001\"\n"
        "  },\n"
        "  {\n"
        "    \"name\": \"Grace Hopper\",\n"
        "    \"address_1\": \"1700 Compiler Lane\",\n"
        "    \"address_2\": \"Arlington\",\n"
        "    \"sku\": \"GH-2002\"\n"
        "  }\n"
        "]\n",
        encoding="utf-8",
    )

    records = load_records(records_path)

    assert records == [
        {
            "name": "Ada Lovelace",
            "address_1": "12 Analytical Engine Way",
            "address_2": "London",
            "sku": "AL-1001",
        },
        {
            "name": "Grace Hopper",
            "address_1": "1700 Compiler Lane",
            "address_2": "Arlington",
            "sku": "GH-2002",
        },
    ]


def test_save_template_rounds_float_values_to_two_decimals(tmp_path: Path) -> None:
    template_path = tmp_path / "template.json"
    template = LabelTemplate(
        page=PageSpec(width_mm=215.89999999999998, height_mm=279.4),
        grid=GridSpec(
            rows=4,
            cols=2,
            margin_left_mm=21.590070555555553,
            margin_top_mm=25.4,
            margin_right_mm=21.590070555555567,
            margin_bottom_mm=25.452704999999987,
            gap_x_mm=20.31985888888889,
            gap_y_mm=8.449098333333332,
            label_width_mm=76.19999999999999,
            label_height_mm=50.8,
        ),
    )

    save_template(template_path, template)
    saved_text = template_path.read_text(encoding="utf-8")

    assert '"width_mm": 215.90' in saved_text
    assert '"margin_left_mm": 21.59' in saved_text
    assert '"margin_right_mm": 21.59' in saved_text
    assert '"margin_bottom_mm": 25.45' in saved_text
    assert '"gap_x_mm": 20.32' in saved_text
    assert '"gap_y_mm": 8.45' in saved_text
    assert '"label_width_mm": 76.20' in saved_text
    assert '"label_height_mm": 50.80' in saved_text
    assert '"rows": 4' in saved_text


def test_save_and_load_text_layout_template(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    layout = TextLayoutTemplate(
        name="address-layout",
        elements=[
            TextElement(
                field="name",
                x_mm=4,
                y_mm=3,
                width_mm=58,
                height_mm=8,
            )
        ],
    )

    save_text_layout_template(layout_path, layout)
    loaded_layout = load_text_layout_template(layout_path)
    saved_text = layout_path.read_text(encoding="utf-8")

    assert loaded_layout.name == "address-layout"
    assert len(loaded_layout.elements) == 1
    assert loaded_layout.elements[0].field == "name"
    assert '"template_type": "text-layout"' in saved_text
    assert '"x_mm": 4.00' in saved_text


def test_repo_minimal_farmhouse_spice_templates_load() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    label_path = repo_root / "templates" / "labels" / "minimal_farmhouse_spice_jar_1p35x1p9.json"
    layout_path = repo_root / "templates" / "layouts" / "minimal_farmhouse_spice_jar_layout.json"

    label_template = load_template(label_path)
    layout_template = load_text_layout_template(layout_path)

    assert label_template.name == "minimal-farmhouse-spice-jar-1p35x1p9"
    assert abs(label_template.page.width_mm - in_to_mm(1.35)) < 1e-6
    assert abs(label_template.page.height_mm - in_to_mm(1.90)) < 1e-6
    assert label_template.grid.rows == 1
    assert label_template.grid.cols == 1
    assert len(label_template.elements) == 3
    assert layout_template.name == "minimal-farmhouse-spice-jar-layout"
    assert [element.field for element in layout_template.elements] == [
        "name",
        "category",
        "descriptor",
    ]
