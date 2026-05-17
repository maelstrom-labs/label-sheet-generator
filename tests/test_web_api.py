from pathlib import Path

import pytest


pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from label_sheet_generator.avery import build_template_from_avery_preset
from label_sheet_generator.io import save_template
from label_sheet_generator.web_api import app


def test_bootstrap_lists_local_templates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "labels" / "sheet.json",
        build_template_from_avery_preset("5160", name="sheet"),
    )
    client = TestClient(app)

    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert payload["label_templates"][0]["key"] == "labels/sheet"
    assert payload["label_templates"][0]["template_name"] == "sheet"


def test_workspace_config_returns_seed_document(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "labels" / "sheet.json",
        build_template_from_avery_preset("5160", name="sheet"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/workspace/config",
        json={"label_template_key": "labels/sheet", "layout_template_key": None},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["label_template_name"] == "sheet"
    assert "schema" in payload["document"]


def test_render_pdf_endpoint_returns_pdf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save_template(
        tmp_path / "templates" / "labels" / "sheet.json",
        build_template_from_avery_preset("5160", name="sheet"),
    )
    client = TestClient(app)
    config = client.post(
        "/api/workspace/config",
        json={"label_template_key": "labels/sheet", "layout_template_key": None},
    ).json()

    response = client.post(
        "/api/render/pdf",
        json={
            "label_template_key": "labels/sheet",
            "layout_template_key": None,
            "data_document": '{"schema": [], "records": [{}]}',
            "margin_top_mm": config["margin_top_mm"],
            "margin_right_mm": config["margin_right_mm"],
            "margin_bottom_mm": config["margin_bottom_mm"],
            "margin_left_mm": config["margin_left_mm"],
            "page_orientation": "portrait",
            "page_rotation_deg": 0,
            "text_rotation_deg": None,
            "outline_slots": True,
            "download_filename": "labels.pdf",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")