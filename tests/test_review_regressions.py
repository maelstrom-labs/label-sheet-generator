"""Regressions for defects found by the post-rebuild adversarial review.

Each test here corresponds to something that was reproduced against the
finished code. They are grouped by the defect rather than by module, because
the point of each is "this specific failure does not come back".
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from pypdf import PdfReader

from label_sheet_generator.api.app import create_app
from label_sheet_generator.api.limits import MAX_CLIENT_KEY_LENGTH, client_key
from label_sheet_generator.errors import LimitExceeded, Overloaded, RenderTimeout
from label_sheet_generator.records import MAX_COLUMNS, MAX_WARNINGS, _build_header
from label_sheet_generator.render import RenderOptions, render_pdf
from label_sheet_generator.schema import LabelTemplate
from label_sheet_generator.settings import Settings, default_settings


def unthrottled(**overrides: object) -> Settings:
    return replace(
        default_settings(),
        rate_limit_burst=100_000,
        rate_limit_per_second=1e6,
        render_rate_limit_burst=100_000,
        render_rate_limit_per_second=1e6,
        **overrides,  # type: ignore[arg-type]
    )


# --- Quadratic CSV header disambiguation ----------------------------------


def test_duplicate_header_dedupe_is_linear() -> None:
    # Restarting the suffix search at 2 for every repeat made this quadratic:
    # 16,000 identical headers cost 63s of CPU for a 64KB upload.
    timings = []
    for count in (128, 256, 512):
        warnings: list[dict[str, object]] = []
        start = time.perf_counter()
        header = _build_header(["a"] * count, warnings)
        timings.append(time.perf_counter() - start)
        assert len(set(header)) == count, "columns must stay distinct"

    assert timings[-1] < 0.5, f"512 duplicate headers took {timings[-1]:.2f}s"


def test_a_column_count_bomb_is_refused() -> None:
    with pytest.raises(LimitExceeded) as excinfo:
        _build_header(["a"] * (MAX_COLUMNS + 1), [])
    assert excinfo.value.limit_name == "max_columns"


def test_warnings_are_capped_so_a_small_upload_cannot_inflate_the_response() -> None:
    warnings: list[dict[str, object]] = []
    _build_header(["a"] * MAX_COLUMNS, warnings)
    assert len(warnings) <= MAX_WARNINGS + 1
    assert warnings[-1]["code"] == "warnings_truncated"


def test_a_duplicate_header_upload_stays_fast_and_small() -> None:
    columns = 400
    payload = (",".join(["a"] * columns) + "\n" + ",".join(["1"] * columns) + "\n").encode()
    with TestClient(create_app(unthrottled())) as client:
        start = time.time()
        response = client.post(
            "/api/records/parse",
            files={"file": ("w.csv", payload, "text/csv")},
            data={"fields": "[]"},
        )
        elapsed = time.time() - start
    assert response.status_code == 200
    assert elapsed < 5.0, f"took {elapsed:.1f}s"
    assert len(response.content) < 200_000, "response amplified out of proportion"


# --- page_rotation_deg dropped content ------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_rotation_never_drops_labels(rotation: int, orientation: str) -> None:
    # reportlab swaps the MediaBox when /Rotate is 90 or 270 but leaves the
    # content stream in the original space, so two rows of a 10-row sheet fell
    # outside the page with no error anywhere.
    template = LabelTemplate.model_validate(
        {
            "page": {"width_mm": 215.9, "height_mm": 279.4},
            "grid": {
                "rows": 10,
                "cols": 3,
                "margin_left_mm": 4.7625,
                "margin_top_mm": 12.7,
                "gap_x_mm": 3.175,
                "label_width_mm": 66.675,
                "label_height_mm": 25.4,
            },
            "elements": [
                {
                    "type": "text",
                    "field": "name",
                    "x_mm": 4,
                    "y_mm": 3,
                    "width_mm": 58,
                    "height_mm": 8,
                }
            ],
        }
    )
    records = [{"name": f"NAME{i:02d}"} for i in range(30)]
    pdf = render_pdf(
        template,
        records,
        options=RenderOptions(page_orientation=orientation, page_rotation_deg=rotation),
    ).pdf_bytes

    page = PdfReader(io.BytesIO(pdf)).pages[0]
    found = set(re.findall(r"NAME\d\d", page.extract_text()))
    assert len(found) == 30, f"{30 - len(found)} labels fell outside the page"

    # The MediaBox must describe the space the content was drawn into.
    expected = (279.4, 215.9) if orientation == "landscape" else (215.9, 279.4)
    width_mm = float(page.mediabox.width) * 25.4 / 72
    height_mm = float(page.mediabox.height) * 25.4 / 72
    assert width_mm == pytest.approx(expected[0], abs=0.1)
    assert height_mm == pytest.approx(expected[1], abs=0.1)
    assert page.get("/Rotate") == rotation


# --- Render slot accounting -----------------------------------------------


def test_a_timed_out_render_keeps_its_slot_until_the_worker_stops() -> None:
    # Future.cancel() cannot stop a running thread. Releasing the slot anyway
    # handed capacity to the next caller while the abandoned render still had
    # a worker, so one oversized request 503'd everything behind it.
    from label_sheet_generator.catalog import Catalog
    from label_sheet_generator.service import LabelSheetService

    settings = replace(
        default_settings(),
        max_concurrent_renders=1,
        render_timeout_s=0.05,
        max_pages=500,
        max_records=20_000,
    )
    service = LabelSheetService(settings, Catalog.build(settings))
    try:
        document = json.dumps(
            {
                "schema": ["name", "address_1", "address_2", "sku"],
                "records": [
                    {
                        "name": f"Person {i}",
                        "address_1": "12 Some Street",
                        "address_2": "Town",
                        "sku": f"SKU-{i:06d}",
                    }
                    for i in range(6000)
                ],
            }
        )
        big = service.plan(template_id="labels/basic-address", layout_id=None, document=document)
        small = service.plan(template_id="labels/basic-address", layout_id=None, document="")

        with pytest.raises(RenderTimeout) as excinfo:
            service.render(big, RenderOptions())
        assert excinfo.value.retry_after >= 1

        # The worker is still busy, so the next request must be shed, not served.
        with pytest.raises(Overloaded):
            service.render(small, RenderOptions())
    finally:
        service.close()


def test_a_render_timeout_is_a_503_not_a_500() -> None:
    # concurrent.futures.TimeoutError is only an alias of the builtin from 3.11;
    # on 3.10 catching the builtin alone let it escape as an internal error.
    settings = unthrottled(render_timeout_s=0.001, max_records=20_000, max_pages=500)
    document = json.dumps({"schema": ["name"], "records": [{"name": f"r{i}"} for i in range(4000)]})
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/render/pdf",
            json={"template_id": "labels/basic-address", "document": document},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "render_timeout"
    assert response.headers.get("retry-after")


# --- Input that used to reach the serializer ------------------------------


@pytest.mark.parametrize("endpoint", ["/api/render/plan", "/api/render/pdf"])
def test_an_unpaired_surrogate_in_a_record_is_a_422(endpoint: str) -> None:
    # json.loads produces one from a \ud800 escape, but it cannot be encoded
    # back to UTF-8, so it crashed response serialisation with a 500.
    document = '{"schema":["name"],"records":[{"name":"bad \\ud800 here"}]}'
    with TestClient(create_app(unthrottled())) as client:
        response = client.post(endpoint, json={"template_id": "avery:5160", "document": document})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "record_error"


@pytest.mark.parametrize("depth", [250, 400])
def test_deeply_nested_template_metadata_is_a_422(depth: int) -> None:
    value: dict[str, object] = {"x": 1}
    for _ in range(depth):
        value = {"n": value}
    with TestClient(create_app(unthrottled())) as client:
        response = client.post(
            "/api/templates/validate",
            json={
                "template": {
                    "page": {"width_mm": 100, "height_mm": 100},
                    "grid": {"rows": 1, "cols": 1},
                    "metadata": value,
                }
            },
        )
    assert response.status_code == 422


def test_shallow_metadata_is_still_accepted() -> None:
    with TestClient(create_app(unthrottled())) as client:
        response = client.post(
            "/api/templates/validate",
            json={
                "template": {
                    "page": {"width_mm": 100, "height_mm": 100},
                    "grid": {"rows": 1, "cols": 1},
                    "metadata": {"source": "a datasheet", "tags": ["a", "b"]},
                }
            },
        )
    assert response.status_code == 200


# --- Rate limiter keying --------------------------------------------------


def test_forwarded_for_split_across_header_lines_cannot_shift_the_bucket() -> None:
    # RFC 7230 allows a repeated field as several lines. Reading only the first
    # let a client send its own X-Forwarded-For and have the proxy append the
    # real one, so every request landed in a different bucket.
    def scope(*values: str) -> dict[str, object]:
        return {
            "headers": [(b"x-forwarded-for", v.encode()) for v in values],
            "client": ("10.0.0.1", 1234),
        }

    joined = client_key(scope("1.1.1.1, 2.2.2.2"), trusted_proxy_hops=1)
    split = client_key(scope("1.1.1.1", "2.2.2.2"), trusted_proxy_hops=1)
    assert joined == split == "2.2.2.2"


def test_the_client_key_is_bounded() -> None:
    key = client_key(
        {"headers": [(b"x-forwarded-for", b"a" * 9000)], "client": None},
        trusted_proxy_hops=1,
    )
    assert len(key) <= MAX_CLIENT_KEY_LENGTH


# --- Body-size limit ------------------------------------------------------


def test_an_oversized_chunked_body_is_413_like_a_declared_one() -> None:
    settings = unthrottled(max_body_bytes=2048, max_document_bytes=1024)
    body = b'{"template_id":"avery:5160","document":"' + b"x" * 20_000 + b'"}'

    def chunks() -> object:
        for index in range(0, len(body), 512):
            yield body[index : index + 512]

    with TestClient(create_app(settings)) as client:
        declared = client.post(
            "/api/render/plan", content=body, headers={"content-type": "application/json"}
        )
        chunked = client.post(
            "/api/render/plan",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    for response in (declared, chunked):
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


# --- Text shrink cost -----------------------------------------------------


def test_shrinking_a_large_font_is_not_quadratic() -> None:
    # Stepping down 0.5pt at a time from the schema maximum of 1000pt meant
    # ~2000 full re-wraps; one 8000-character element took nearly three minutes.
    template = LabelTemplate.model_validate(
        {
            "page": {"width_mm": 100, "height_mm": 60},
            "grid": {"rows": 1, "cols": 1},
            "elements": [
                {
                    "type": "text",
                    "field": "t",
                    "x_mm": 2,
                    "y_mm": 2,
                    "width_mm": 40,
                    "height_mm": 10,
                    "font_size_pt": 1000,
                    "overflow": "shrink",
                }
            ],
        }
    )
    start = time.perf_counter()
    render_pdf(template, [{"t": "x " * 4000}], options=RenderOptions())
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"one element took {elapsed:.1f}s"


# --- Preview bounds -------------------------------------------------------


def test_concurrent_previews_are_bounded_by_the_render_slots() -> None:
    import label_sheet_generator.service as service_module

    overlap = {"now": 0, "peak": 0}
    lock = threading.Lock()
    original = service_module.render_page_png

    def traced(*args: object, **kwargs: object) -> bytes:
        with lock:
            overlap["now"] += 1
            overlap["peak"] = max(overlap["peak"], overlap["now"])
        try:
            time.sleep(0.05)
            return original(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            with lock:
                overlap["now"] -= 1

    service_module.render_page_png = traced  # type: ignore[assignment]
    try:
        settings = unthrottled(max_concurrent_renders=2)
        with TestClient(create_app(settings)) as client:
            threads = [
                threading.Thread(
                    target=lambda: client.post(
                        "/api/render/preview",
                        json={"template_id": "avery:5160", "document": ""},
                    )
                )
                for _ in range(10)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        service_module.render_page_png = original  # type: ignore[assignment]

    # Rasterisation allocates a full-page bitmap; running it outside the slot
    # let every concurrent preview allocate at once.
    assert overlap["peak"] <= 2, f"{overlap['peak']} rasterisations overlapped"


# --- Docs and CSP ---------------------------------------------------------


def test_the_app_keeps_a_strict_csp_while_the_docs_page_works() -> None:
    with TestClient(create_app(unthrottled())) as client:
        index = client.get("/")
        assert "unsafe-inline" not in index.headers["content-security-policy"]
        assert "jsdelivr" not in index.headers["content-security-policy"]

        docs = client.get("/api/docs")
        assert docs.status_code == 200
        # The one page that needs the network says so in its own policy.
        assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]

        assert client.get("/api/openapi.json").status_code == 200


# --- Load shedding is not logged as a fault -------------------------------


def test_shedding_load_does_not_log_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    settings = unthrottled(render_timeout_s=0.001, max_records=20_000, max_pages=500)
    document = json.dumps({"schema": ["name"], "records": [{"name": f"r{i}"} for i in range(4000)]})
    with caplog.at_level(logging.WARNING), TestClient(create_app(settings)) as client:
        client.post(
            "/api/render/pdf",
            json={"template_id": "labels/basic-address", "document": document},
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"load shedding logged at ERROR: {[r.message for r in errors]}"
