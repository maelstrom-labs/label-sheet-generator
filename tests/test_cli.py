from pathlib import Path
from math import isclose

import label_sheet_generator.cli as cli
from label_sheet_generator.avery import build_template_from_avery_preset
from label_sheet_generator.io import save_template, save_text_layout_template
from label_sheet_generator.models import TextElement, TextLayoutTemplate
from label_sheet_generator.units import in_to_mm


def test_list_templates_includes_local_templates(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "custom-template.json",
        build_template_from_avery_preset("5160", name="imported-template"),
    )
    save_text_layout_template(
        tmp_path / "templates" / "layouts" / "custom-layout.json",
        TextLayoutTemplate(name="custom-layout"),
    )

    exit_code = cli.main(["list-templates"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "5160, 5260, 5960, 8160: Address Labels, 1 x 2-5/8 in, 30 per sheet" in captured.out
    assert "local label templates:" in captured.out
    assert "custom-template: imported-template (templates/custom-template.json)" in captured.out
    assert "local text layout templates:" in captured.out
    assert "layouts/custom-layout: custom-layout (templates/layouts/custom-layout.json)" in captured.out


def test_list_templates_alias_still_works(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(["list-presets"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "5160, 5260, 5960, 8160: Address Labels, 1 x 2-5/8 in, 30 per sheet" in captured.out


def test_generate_resolves_templates_from_templates_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "basic.json",
        build_template_from_avery_preset("5163", name="basic"),
    )

    captured_call: dict[str, Path | bool | str | float | int | None] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["output_path"] = output_path
        captured_call["template_path"] = template_path
        captured_call["records_path"] = records_path
        captured_call["outline_slots"] = outline_slots
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "basic", "out.pdf"])

    assert exit_code == 0
    assert captured_call == {
        "output_path": Path("out.pdf"),
        "template_path": Path("templates/basic.json"),
        "records_path": None,
        "outline_slots": False,
        "page_orientation": "portrait",
        "page_rotation_deg": 0,
        "text_rotation_deg": None,
    }


def test_generate_accepts_draw_border_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "basic.json",
        build_template_from_avery_preset("5163", name="basic"),
    )

    captured_call: dict[str, Path | bool | str | float | int | None] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["output_path"] = output_path
        captured_call["template_path"] = template_path
        captured_call["records_path"] = records_path
        captured_call["outline_slots"] = outline_slots
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "basic", "out.pdf", "--draw-border"])

    assert exit_code == 0
    assert captured_call == {
        "output_path": Path("out.pdf"),
        "template_path": Path("templates/basic.json"),
        "records_path": None,
        "outline_slots": True,
        "page_orientation": "portrait",
        "page_rotation_deg": 0,
        "text_rotation_deg": None,
    }


def test_generate_accepts_landscape_orientation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "basic.json",
        build_template_from_avery_preset("5163", name="basic"),
    )

    captured_call: dict[str, Path | bool | str | float | int | None] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["output_path"] = output_path
        captured_call["template_path"] = template_path
        captured_call["records_path"] = records_path
        captured_call["outline_slots"] = outline_slots
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "basic", "out.pdf", "--orientation", "landscape"])

    assert exit_code == 0
    assert captured_call == {
        "output_path": Path("out.pdf"),
        "template_path": Path("templates/basic.json"),
        "records_path": None,
        "outline_slots": False,
        "page_orientation": "landscape",
        "page_rotation_deg": 0,
        "text_rotation_deg": None,
    }


def test_generate_accepts_page_rotation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "basic.json",
        build_template_from_avery_preset("5163", name="basic"),
    )

    captured_call: dict[str, Path | bool | str | float | int | None] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["output_path"] = output_path
        captured_call["template_path"] = template_path
        captured_call["records_path"] = records_path
        captured_call["outline_slots"] = outline_slots
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "basic", "out.pdf", "--orientation", "landscape", "--page-rotation", "180"])

    assert exit_code == 0
    assert captured_call == {
        "output_path": Path("out.pdf"),
        "template_path": Path("templates/basic.json"),
        "records_path": None,
        "outline_slots": False,
        "page_orientation": "landscape",
        "page_rotation_deg": 180,
        "text_rotation_deg": None,
    }


def test_generate_accepts_text_rotation_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "basic.json",
        build_template_from_avery_preset("5163", name="basic"),
    )

    captured_call: dict[str, Path | bool | str | float | int | None] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["output_path"] = output_path
        captured_call["template_path"] = template_path
        captured_call["records_path"] = records_path
        captured_call["outline_slots"] = outline_slots
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "basic", "out.pdf", "--text-rotation", "90"])

    assert exit_code == 0
    assert captured_call == {
        "output_path": Path("out.pdf"),
        "template_path": Path("templates/basic.json"),
        "records_path": None,
        "outline_slots": False,
        "page_orientation": "portrait",
        "page_rotation_deg": 0,
        "text_rotation_deg": 90.0,
    }


def test_import_template_saves_bare_output_into_templates_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_import_template_from_pdf(*args, **kwargs):
        return build_template_from_avery_preset("5160", name="imported")

    monkeypatch.setattr(cli, "import_template_from_pdf", fake_import_template_from_pdf)

    exit_code = cli.main(["import-template", "source.pdf", "imported.json"])

    assert exit_code == 0
    assert (tmp_path / "templates" / "labels" / "imported.json").exists()


def test_generate_applies_text_layout_template(tmp_path: Path, monkeypatch) -> None:
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
                TextElement(
                    field="name",
                    x_mm=4,
                    y_mm=3,
                    width_mm=58,
                    height_mm=8,
                )
            ],
        ),
    )

    captured_call: dict[str, object] = {}

    def fake_render_pdf(template, records, output_path, *, template_path, records_path, outline_slots, page_orientation, page_rotation_deg, text_rotation_deg):
        captured_call["template_path"] = template_path
        captured_call["elements"] = template.elements
        captured_call["page_orientation"] = page_orientation
        captured_call["page_rotation_deg"] = page_rotation_deg
        captured_call["text_rotation_deg"] = text_rotation_deg

    monkeypatch.setattr(cli, "render_pdf", fake_render_pdf)

    exit_code = cli.main(["generate", "sheet", "out.pdf", "--layout-template", "address-layout"])

    assert exit_code == 0
    assert captured_call["template_path"] == Path("templates/layouts/address-layout.json")
    assert len(captured_call["elements"]) == 1
    assert captured_call["elements"][0].field == "name"
    assert captured_call["page_orientation"] == "portrait"
    assert captured_call["page_rotation_deg"] == 0
    assert captured_call["text_rotation_deg"] is None


def test_import_template_accepts_inch_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    captured_kwargs: dict[str, object] = {}

    def fake_import_template_from_pdf(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return build_template_from_avery_preset("5160", name="imported")

    monkeypatch.setattr(cli, "import_template_from_pdf", fake_import_template_from_pdf)

    exit_code = cli.main(
        [
            "import-template",
            "source.pdf",
            "imported.json",
            "--label-width-in",
            "2.625",
            "--label-height-in",
            "1",
            "--margin-left-in",
            "0.1875",
            "--margin-top-in",
            "0.5",
            "--gap-x-in",
            "0.125",
            "--gap-y-in",
            "0",
        ]
    )

    assert exit_code == 0
    assert isclose(captured_kwargs["label_width_mm"], in_to_mm(2.625))
    assert isclose(captured_kwargs["label_height_mm"], in_to_mm(1.0))
    assert isclose(captured_kwargs["margin_left_mm"], in_to_mm(0.1875))
    assert isclose(captured_kwargs["margin_top_mm"], in_to_mm(0.5))
    assert isclose(captured_kwargs["gap_x_mm"], in_to_mm(0.125))
    assert isclose(captured_kwargs["gap_y_mm"], 0.0)


def test_import_template_rejects_mixed_unit_overrides(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "import-template",
            "source.pdf",
            "imported.json",
            "--label-width-mm",
            "66.675",
            "--label-width-in",
            "2.625",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--label-width accepts either millimeters or inches, not both" in captured.err