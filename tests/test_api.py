"""The HTTP surface: what the frontend and any other client can rely on.

Security regressions (CORS, CSP, traversal, rate limiting, body size, header
injection) live in test_security.py; this file is about functional behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import label_sheet_generator
from conftest import render_payload
from label_sheet_generator import __version__
from label_sheet_generator.avery import iter_presets

PACKAGE_ROOT = Path(label_sheet_generator.__file__).resolve().parent
TEMPLATE_DATA = PACKAGE_ROOT / "data" / "templates"

#: Everything the frontend needs in order to draw a sheet thumbnail to scale.
THUMBNAIL_KEYS = {
    "page_width_mm",
    "page_height_mm",
    "rows",
    "cols",
    "label_width_mm",
    "label_height_mm",
    "gap_x_mm",
    "gap_y_mm",
    "margin_top_mm",
    "margin_right_mm",
    "margin_bottom_mm",
    "margin_left_mm",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

CSV_UPLOAD = b"name,address_1,address_2,sku\nAda,12 Analytical Way,London,AL-1001\n"


def shipped_label_ids() -> list[str]:
    """Built-in label template ids, read from the packaged data directory."""
    return sorted(f"labels/{path.stem}" for path in (TEMPLATE_DATA / "labels").glob("*.json"))


def shipped_preset_ids() -> list[str]:
    return sorted(f"avery:{preset.code.lower()}" for preset in iter_presets())


def snapshot(root: Path) -> dict[str, tuple[int, float]]:
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def error_of(response: Any) -> dict[str, Any]:
    return response.json()["error"]


# --- operations -----------------------------------------------------------


def test_livez_answers_ok_while_the_process_is_alive(client) -> None:
    response = client.get("/api/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_every_dependency_as_ready(client) -> None:
    response = client.get("/api/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {
        "catalog_loaded": True,
        "label_templates": True,
        "frontend_assets": True,
    }


def test_version_reports_the_installed_package_version(client) -> None:
    body = client.get("/api/version").json()
    assert body["version"] == __version__
    assert body["python"].count(".") == 2


# --- frontend -------------------------------------------------------------


def test_root_serves_the_single_page_frontend(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text.lower()


@pytest.mark.parametrize("path", ["/static/app.js", "/static/app.css"])
def test_static_frontend_assets_are_served(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.content


# --- bootstrap ------------------------------------------------------------


def test_bootstrap_returns_every_section_the_first_frame_needs(client) -> None:
    body = client.get("/api/bootstrap").json()
    assert set(body) >= {
        "templates",
        "layouts",
        "broken",
        "limits",
        "fonts",
        "features",
        "version",
    }


def test_bootstrap_lists_every_shipped_label_template_and_preset(client) -> None:
    listed = {entry["id"] for entry in client.get("/api/bootstrap").json()["templates"]}
    assert listed >= set(shipped_label_ids())
    assert listed >= set(shipped_preset_ids())


def test_bootstrap_lists_the_shipped_text_layouts_separately_from_the_sheets(client) -> None:
    body = client.get("/api/bootstrap").json()
    layout_ids = {entry["id"] for entry in body["layouts"]}
    assert layout_ids == {
        f"layouts/{path.stem}" for path in (TEMPLATE_DATA / "layouts").glob("*.json")
    }
    assert layout_ids.isdisjoint({entry["id"] for entry in body["templates"]})


def test_every_listed_template_carries_the_geometry_a_thumbnail_needs(client) -> None:
    for entry in client.get("/api/bootstrap").json()["templates"]:
        assert entry["geometry"] is not None, entry["id"]
        assert set(entry["geometry"]) >= THUMBNAIL_KEYS, entry["id"]
        assert entry["geometry"]["page_width_mm"] > 0
        assert entry["labels_per_page"] >= 1


def test_no_shipped_template_fails_to_load(client) -> None:
    # A file that would not parse used to vanish from the listing silently;
    # it is now reported in "broken" with the reason, so an empty list here
    # is a real assertion that the packaged data is loadable.
    assert client.get("/api/bootstrap").json()["broken"] == []


def test_bootstrap_publishes_the_limits_the_frontend_pre_validates_against(client) -> None:
    limits = client.get("/api/bootstrap").json()["limits"]
    assert limits["max_records"] > 0
    assert limits["max_pages"] > 0
    assert limits["preview_scale_min"] <= limits["preview_scale_default"]
    assert limits["preview_scale_default"] <= limits["preview_scale_max"]


def test_bootstrap_lists_selectable_fonts(client) -> None:
    fonts = client.get("/api/bootstrap").json()["fonts"]
    assert "Helvetica" in fonts


def test_bootstrap_version_agrees_with_the_version_endpoint(client) -> None:
    body = client.get("/api/bootstrap").json()
    assert body["version"]["version"] == client.get("/api/version").json()["version"]


# --- catalog --------------------------------------------------------------


def test_builtin_template_is_returned_with_its_fields_and_geometry(
    client, basic_template_id: str
) -> None:
    body = client.get(f"/api/templates/{basic_template_id}").json()
    assert body["kind"] == "label"
    assert body["units"] == "mm"
    assert body["fields"] == ["name", "address_1", "address_2", "sku"]
    assert body["template"]["grid"]["rows"] >= 1
    assert set(body["geometry"]) >= THUMBNAIL_KEYS


def test_avery_preset_is_reachable_as_a_template(client) -> None:
    body = client.get("/api/templates/avery:5160").json()
    assert body["kind"] == "label"
    assert (body["template"]["grid"]["rows"], body["template"]["grid"]["cols"]) == (10, 3)


def test_avery_preset_is_reachable_by_an_alias_code(client) -> None:
    by_code = client.get("/api/templates/avery:5160").json()
    by_alias = client.get("/api/templates/avery:5260").json()
    assert by_alias["template"] == by_code["template"]


def test_unknown_template_id_is_not_found(client) -> None:
    response = client.get("/api/templates/labels/does-not-exist")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


@pytest.mark.parametrize("kind", ["label", "text-layout"])
def test_template_listing_can_be_filtered_by_kind(client, kind: str) -> None:
    entries = client.get("/api/templates", params={"kind": kind}).json()["templates"]
    assert entries
    assert {entry["kind"] for entry in entries} == {kind}


def test_template_listing_rejects_an_unknown_kind(client) -> None:
    response = client.get("/api/templates", params={"kind": "sticker"})
    assert response.status_code == 422
    assert "label" in error_of(response)["message"]


# --- template validation --------------------------------------------------


def test_validate_accepts_a_shipped_template_document(client, basic_template_id: str) -> None:
    document = client.get(f"/api/templates/{basic_template_id}").json()["template"]
    body = client.post("/api/templates/validate", json={"template": document}).json()
    assert body["ok"] is True
    assert body["units"] == "mm"
    assert body["fields"] == ["name", "address_1", "address_2", "sku"]
    assert body["geometry"]["ok"] is True
    assert body["normalized"]["grid"]["rows"] == document["grid"]["rows"]


def test_validate_keeps_an_inch_authored_template_in_inches(client) -> None:
    document = client.get("/api/templates/labels/basic-address-inch").json()["template"]
    body = client.post("/api/templates/validate", json={"template": document}).json()
    assert body["units"] == "in"
    assert "width_in" in body["normalized"]["page"]


def test_validate_reports_a_geometry_problem_without_rejecting_the_document(client) -> None:
    body = client.post(
        "/api/templates/validate",
        json={
            "template": {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 2, "cols": 2, "label_width_mm": 60, "label_height_mm": 10},
            }
        },
    ).json()
    assert body["ok"] is True
    assert body["geometry"]["ok"] is False
    assert body["geometry"]["errors"][0]["code"] == "grid_exceeds_page"


def test_validate_rejects_a_bad_template_with_field_level_details(client) -> None:
    response = client.post(
        "/api/templates/validate",
        json={"template": {"page": {"width_mm": -5}, "grid": {"rows": 0, "cols": 1}}},
    )
    assert response.status_code == 422
    details = error_of(response)["details"]
    assert details
    assert ["page", "width_mm"] in [detail["loc"] for detail in details]


def test_validate_rejects_a_document_that_is_not_a_template_at_all(client) -> None:
    response = client.post("/api/templates/validate", json={"template": {"colour": "red"}})
    assert response.status_code == 422
    assert "template_type" in error_of(response)["message"]


# --- record uploads -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "payload", "expected_format"),
    [
        ("rows.csv", CSV_UPLOAD, "delimited"),
        ("rows.tsv", b"name\tsku\nAda\tAL-1001\n", "delimited"),
        (
            "rows.json",
            b'[{"name": "Ada", "sku": "AL-1001"}]',
            "json",
        ),
    ],
)
def test_upload_is_parsed_into_the_canonical_document(
    client, filename: str, payload: bytes, expected_format: str
) -> None:
    response = client.post("/api/records/parse", files={"file": (filename, payload)})
    assert response.status_code == 200
    body = response.json()
    assert body["detected"]["format"] == expected_format
    assert body["record_count"] == 1
    assert "name" in body["schema"]
    assert json.loads(body["document"])["records"][0]["name"] == "Ada"
    assert body["warnings"] == []


def test_upload_parse_orders_the_schema_around_the_requested_fields(client) -> None:
    response = client.post(
        "/api/records/parse",
        files={"file": ("rows.csv", b"sku,name\nAL-1001,Ada\n")},
        data={"fields": json.dumps(["name", "sku"])},
    )
    assert response.json()["schema"] == ["name", "sku"]


def test_upload_parse_reports_ragged_rows_as_warnings_rather_than_failing(client) -> None:
    response = client.post(
        "/api/records/parse",
        files={"file": ("ragged.csv", b"name,sku\nAda\nAlan,X2,surplus\n")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["record_count"] == 2
    codes = {warning["code"] for warning in body["warnings"]}
    assert codes == {"row_underflow", "row_overflow"}


def test_empty_upload_is_rejected(client) -> None:
    response = client.post("/api/records/parse", files={"file": ("empty.csv", b"")})
    assert response.status_code == 422
    assert "empty" in error_of(response)["message"]


@pytest.mark.parametrize("fields", ["not-json", '{"name": 1}', '"name"'])
def test_upload_parse_rejects_a_fields_value_that_is_not_a_json_array(client, fields: str) -> None:
    response = client.post(
        "/api/records/parse",
        files={"file": ("rows.csv", CSV_UPLOAD)},
        data={"fields": fields},
    )
    assert response.status_code == 422
    assert error_of(response)["loc"] == ["fields"]


def test_upload_parse_requires_a_file(client) -> None:
    response = client.post("/api/records/parse", data={"fields": "[]"})
    assert response.status_code == 422
    assert ["body", "file"] in [detail["loc"] for detail in error_of(response)["details"]]


# --- render plan ----------------------------------------------------------


def test_plan_counts_labels_pages_and_slots(client, basic_template_id, sample_document) -> None:
    body = client.post(
        "/api/render/plan", json=render_payload(basic_template_id, sample_document)
    ).json()
    assert body["labels"] == 2
    assert body["labels_per_page"] == 30
    assert body["pages"] == 1
    assert body["missing_fields"] == []
    assert body["extra_fields"] == []


def test_plan_includes_a_geometry_report(client, basic_template_id, sample_document) -> None:
    geometry = client.post(
        "/api/render/plan", json=render_payload(basic_template_id, sample_document)
    ).json()["geometry"]
    assert geometry["ok"] is True
    assert geometry["label_width_mm"] > 0
    assert geometry["errors"] == []


def test_plan_surfaces_fields_the_document_does_not_supply(client, sample_document) -> None:
    body = client.post(
        "/api/render/plan", json=render_payload("labels/spice-jar", sample_document)
    ).json()
    assert set(body["missing_fields"]) == set(body["fields"]) - {"name"}
    assert "sku" in body["extra_fields"]


def test_plan_of_an_empty_document_still_reports_one_page(client, basic_template_id) -> None:
    body = client.post("/api/render/plan", json=render_payload(basic_template_id)).json()
    assert body["labels"] == 0
    assert body["pages"] == 1


def test_plan_applies_a_layout_template_over_the_sheet(
    client, basic_template_id, sample_document
) -> None:
    body = client.post(
        "/api/render/plan",
        json=render_payload(basic_template_id, sample_document, layout_id="layouts/basic-address"),
    ).json()
    assert body["layout_name"] != "Default"
    assert body["labels_per_page"] == 30


def test_plan_applies_margin_overrides_to_the_grid(
    client, basic_template_id, sample_document
) -> None:
    body = client.post(
        "/api/render/plan",
        json=render_payload(basic_template_id, sample_document, overrides={"margin_top_mm": 200}),
    ).json()
    assert body["geometry"]["ok"] is False
    assert body["labels_per_page"] == 0


@pytest.mark.parametrize(
    ("payload", "expected_loc"),
    [
        ({"template_id": "layouts/spice-jar"}, ["template_id"]),
        ({"template_id": "labels/basic-address", "layout_id": "labels/spice-jar"}, ["layout_id"]),
    ],
)
def test_plan_rejects_a_template_and_layout_swapped_round(
    client, payload: dict[str, Any], expected_loc: list[str]
) -> None:
    response = client.post("/api/render/plan", json={**payload, "document": ""})
    assert response.status_code == 422
    assert error_of(response)["loc"] == expected_loc


def test_plan_rejects_a_document_that_is_not_json(client, basic_template_id) -> None:
    response = client.post("/api/render/plan", json=render_payload(basic_template_id, "{not json"))
    assert response.status_code == 422
    assert "JSON" in error_of(response)["message"]


def test_plan_rejects_more_records_than_the_configured_maximum(
    client, basic_template_id, settings
) -> None:
    document = json.dumps(
        {
            "schema": ["name"],
            "records": [{"name": f"n{index}"} for index in range(settings.max_records + 1)],
        }
    )
    response = client.post("/api/render/plan", json=render_payload(basic_template_id, document))
    assert response.status_code == 422
    body = error_of(response)
    assert body["code"] == "limit_exceeded"
    assert body["limit"] == settings.max_records


def test_plan_of_an_unknown_template_is_not_found(client, sample_document) -> None:
    response = client.post("/api/render/plan", json=render_payload("labels/ghost", sample_document))
    assert response.status_code == 404


# --- preview --------------------------------------------------------------


def test_preview_returns_png_bytes_and_not_base64_json(
    client, basic_template_id, sample_document
) -> None:
    # The preview used to be a JSON body with a base64 data URL inside it,
    # which inflated every frame by a third and forced the browser to decode
    # it in JavaScript. The contract is now raw image bytes.
    response = client.post(
        "/api/render/preview", json=render_payload(basic_template_id, sample_document)
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(PNG_MAGIC)
    with pytest.raises(ValueError):
        response.json()


def test_preview_reports_the_page_and_label_counts_in_headers(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/preview", json=render_payload(basic_template_id, sample_document)
    )
    assert response.headers["x-page-count"] == "1"
    assert response.headers["x-label-count"] == "2"


def test_preview_returns_304_for_a_matching_if_none_match(
    client, basic_template_id, sample_document
) -> None:
    payload = render_payload(basic_template_id, sample_document)
    first = client.post("/api/render/preview", json=payload)
    etag = first.headers["etag"]
    second = client.post("/api/render/preview", json=payload, headers={"if-none-match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""


def test_preview_re_renders_when_the_etag_does_not_match(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/preview",
        json=render_payload(basic_template_id, sample_document),
        headers={"if-none-match": '"stale"'},
    )
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)


def test_preview_etag_changes_when_the_request_changes(
    client, basic_template_id, sample_document
) -> None:
    first = client.post(
        "/api/render/preview", json=render_payload(basic_template_id, sample_document)
    )
    second = client.post(
        "/api/render/preview",
        json=render_payload(basic_template_id, sample_document, scale=2.0),
    )
    assert first.headers["etag"] != second.headers["etag"]
    assert len(first.content) != len(second.content)


def test_preview_clamps_a_page_past_the_end_of_the_document(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/preview", json=render_payload(basic_template_id, sample_document, page=99)
    )
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)


@pytest.mark.parametrize("scale", [0, -1, 99])
def test_preview_rejects_an_out_of_range_scale(
    client, basic_template_id, sample_document, scale: float
) -> None:
    response = client.post(
        "/api/render/preview",
        json=render_payload(basic_template_id, sample_document, scale=scale),
    )
    assert response.status_code == 422
    assert ["body", "scale"] in [detail["loc"] for detail in error_of(response)["details"]]


# --- pdf ------------------------------------------------------------------


def test_pdf_render_returns_a_pdf_with_its_counts(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/pdf", json=render_payload(basic_template_id, sample_document)
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert response.headers["x-label-count"] == "2"
    assert response.headers["x-page-count"] == "1"


def test_pdf_render_offers_a_download_filename(client, basic_template_id, sample_document) -> None:
    response = client.post(
        "/api/render/pdf",
        json=render_payload(basic_template_id, sample_document, filename="spring-mailing"),
    )
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="spring-mailing.pdf"' in disposition


def test_pdf_render_defaults_the_filename_when_none_is_given(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/pdf", json=render_payload(basic_template_id, sample_document)
    )
    assert ".pdf" in response.headers["content-disposition"]


def test_pdf_render_of_a_preset_template_succeeds(client, sample_document) -> None:
    response = client.post("/api/render/pdf", json=render_payload("avery:5160", sample_document))
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


def test_pdf_render_refuses_a_grid_that_does_not_fit_the_page(
    client, basic_template_id, sample_document
) -> None:
    response = client.post(
        "/api/render/pdf",
        json=render_payload(basic_template_id, sample_document, overrides={"margin_top_mm": 200}),
    )
    assert response.status_code == 422
    assert error_of(response)["details"][0]["code"] == "grid_exceeds_page"


# --- presets --------------------------------------------------------------


def test_avery_presets_are_listed_with_the_details_the_picker_shows(client) -> None:
    presets = client.get("/api/presets/avery").json()["presets"]
    assert {preset["code"] for preset in presets} == {p.code for p in iter_presets()}
    first = presets[0]
    assert first["rows"] >= 1 and first["cols"] >= 1
    assert first["stock"]
    assert first["description"]


def test_materializing_a_preset_returns_a_usable_template_document(client) -> None:
    template = client.post("/api/presets/avery/5160/template").json()["template"]
    assert (template["grid"]["rows"], template["grid"]["cols"]) == (10, 3)
    validated = client.post("/api/templates/validate", json={"template": template})
    assert validated.status_code == 200
    assert validated.json()["ok"] is True


def test_materializing_a_preset_writes_nothing_to_disk(client, settings) -> None:
    before = snapshot(settings.builtin_template_root)
    assert client.post("/api/presets/avery/5163/template").status_code == 200
    assert snapshot(settings.builtin_template_root) == before


@pytest.mark.parametrize("code", ["9999", "nope", "../etc"])
def test_an_unknown_avery_code_is_not_found(client, code: str) -> None:
    response = client.post(f"/api/presets/avery/{code}/template")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# --- error envelope and request ids ---------------------------------------


def request_cases() -> list[tuple[str, str, dict[str, Any], int]]:
    return [
        ("get", "/api/templates/labels/ghost", {}, 404),
        ("get", "/api/nowhere", {}, 404),
        ("get", "/api/render/pdf", {}, 405),
        ("post", "/api/render/plan", {"json": {}}, 422),
        ("post", "/api/templates/validate", {"json": {"template": {}}}, 422),
        ("post", "/api/presets/avery/0000/template", {}, 404),
    ]


@pytest.mark.parametrize(("method", "path", "kwargs", "status"), request_cases())
def test_every_error_response_uses_the_same_envelope(
    client, method: str, path: str, kwargs: dict[str, Any], status: int
) -> None:
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert {"code", "message", "request_id", "details"} <= set(body["error"])
    assert isinstance(body["error"]["details"], list)
    assert body["error"]["message"]


def test_an_unknown_template_id_reports_the_not_found_code(client) -> None:
    response = client.get("/api/templates/labels/ghost")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_a_malformed_body_reports_validation_error_with_details(client) -> None:
    response = client.post("/api/render/plan", json={"template_id": "", "nonsense": True})
    assert response.status_code == 422
    body = error_of(response)
    assert body["code"] == "validation_error"
    assert body["details"]
    assert all({"loc", "msg", "type"} <= set(detail) for detail in body["details"])


def test_error_details_never_echo_the_submitted_value(client) -> None:
    # The old handler returned pydantic's raw errors, which include "input"
    # and "ctx" and so reflected the caller's payload straight back.
    response = client.post(
        "/api/render/plan", json={"template_id": "x", "document": "y", "secret": "hunter2"}
    )
    assert "hunter2" not in response.text
    assert all(set(detail) == {"loc", "msg", "type"} for detail in error_of(response)["details"])


def test_request_id_is_echoed_when_the_caller_supplies_one(client) -> None:
    response = client.get("/api/version", headers={"x-request-id": "trace-0001"})
    assert response.headers["x-request-id"] == "trace-0001"


def test_request_id_is_minted_when_the_caller_supplies_none(client) -> None:
    first = client.get("/api/version").headers["x-request-id"]
    second = client.get("/api/version").headers["x-request-id"]
    assert first and second and first != second


def test_a_hostile_request_id_is_replaced_rather_than_echoed(client) -> None:
    response = client.get("/api/version", headers={"x-request-id": "bad id with spaces"})
    assert response.headers["x-request-id"] != "bad id with spaces"


def test_the_error_body_quotes_the_same_request_id_as_the_header(client) -> None:
    response = client.get("/api/templates/labels/ghost", headers={"x-request-id": "trace-0002"})
    assert error_of(response)["request_id"] == "trace-0002"
    assert response.headers["x-request-id"] == "trace-0002"
