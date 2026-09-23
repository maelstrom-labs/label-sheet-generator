"""Security regression suite.

Every test here corresponds to a defect that was confirmed exploitable against
the previous implementation, or to a control the rebuild depends on. They are
first-class tests, not documentation: if one starts failing, a real protection
has been removed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from label_sheet_generator.errors import AssetError, NotFoundError, UnsafePathError
from label_sheet_generator.settings import Settings

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from label_sheet_generator.api.app import create_app
from label_sheet_generator.api.routes import content_disposition, sanitize_filename

# ---------------------------------------------------------------------------
# Arbitrary file read through record-supplied image references
# ---------------------------------------------------------------------------


def _template_with_image(tmp_path: Path) -> Path:
    """A user template whose only element is a record-driven image."""
    root = tmp_path / "templates" / "labels"
    root.mkdir(parents=True)
    (root / "img.json").write_text(
        json.dumps(
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": [
                    {
                        "type": "image",
                        "field": "logo",
                        "x_mm": 1,
                        "y_mm": 1,
                        "width_mm": 20,
                        "height_mm": 20,
                    }
                ],
            }
        )
    )
    return tmp_path / "templates"


HOSTILE_REFERENCES = [
    "/etc/passwd",
    "/etc/hostname",
    "../../../../etc/passwd",
    "../secret.txt",
    "../secret.png",
    "subdir/../../secret.png",
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data/",
    "https://example.com/logo.png",
    "logo.png\x00.txt",
    "....//....//secret.png",
]


@pytest.mark.parametrize("reference", HOSTILE_REFERENCES)
def test_asset_loader_refuses_every_escape(asset_dir: Path, reference: str) -> None:
    # CONFIRMED against the old code: a record of {"logo": "/tmp/secret.png"}
    # returned HTTP 200 and a PDF with the out-of-tree file embedded.
    from label_sheet_generator.assets import AssetLoader

    loader = AssetLoader(asset_dir, max_bytes=1_000_000, max_pixels=1_000_000)
    with pytest.raises((AssetError, UnsafePathError, NotFoundError)):
        loader.load(reference)


def test_asset_loader_accepts_a_legitimate_reference(asset_dir: Path) -> None:
    from label_sheet_generator.assets import AssetLoader

    loader = AssetLoader(asset_dir, max_bytes=1_000_000, max_pixels=1_000_000)
    assert loader.load("logo.png") is not None


def test_asset_errors_never_disclose_the_resolved_path(asset_dir: Path) -> None:
    # The loader is the obvious file-existence oracle, so its messages must not
    # differentiate by leaking where it looked.
    from label_sheet_generator.assets import AssetLoader

    loader = AssetLoader(asset_dir, max_bytes=1_000_000, max_pixels=1_000_000)
    with pytest.raises((AssetError, NotFoundError)) as excinfo:
        loader.load("nope.png")
    message = str(excinfo.value)
    assert str(asset_dir) not in message
    assert "/tmp" not in message


def test_images_are_disabled_entirely_when_no_asset_root_is_configured() -> None:
    # The safe default for a public instance: with no LSG_ASSET_DIR there is no
    # directory an image element could legitimately read from, so the feature
    # is off rather than pointed at the filesystem root.
    from label_sheet_generator.assets import AssetLoader

    loader = AssetLoader(None, max_bytes=1_000_000, max_pixels=1_000_000)
    assert not loader.enabled
    with pytest.raises(AssetError):
        loader.load("logo.png")


def test_render_endpoint_cannot_read_a_file_outside_the_asset_root(
    settings: Settings, tmp_path: Path, asset_dir: Path
) -> None:
    template_root = _template_with_image(tmp_path)
    hardened = replace(
        settings,
        user_template_root=template_root,
        asset_root=asset_dir,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    with TestClient(create_app(hardened)) as client:
        for reference in ("/etc/passwd", "../secret.txt", "../secret.png"):
            response = client.post(
                "/api/render/pdf",
                json={
                    "template_id": "labels/img",
                    "document": json.dumps({"schema": ["logo"], "records": [{"logo": reference}]}),
                },
            )
            # A bad reference degrades to a warning rather than failing the
            # sheet, so the property that matters is that nothing was read --
            # and that the caller is told, rather than it happening silently.
            assert response.status_code < 500, f"{reference} produced a {response.status_code}"
            assert b"TOP SECRET" not in response.content
            assert b"root:x:" not in response.content
            if response.status_code == 200:
                assert int(response.headers["x-render-warnings"]) >= 1, (
                    f"{reference} failed silently"
                )

        ok = client.post(
            "/api/render/pdf",
            json={
                "template_id": "labels/img",
                "document": json.dumps({"schema": ["logo"], "records": [{"logo": "logo.png"}]}),
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.headers["x-render-warnings"] == "0"


def test_reportlab_url_fetching_is_pinned_off() -> None:
    # reportlab's image loader falls back to fetching a URL. The upstream
    # default for trustedHosts has changed before; this app must not depend on
    # it, so assets.py pins both lists empty at import.
    from reportlab import rl_config

    import label_sheet_generator.assets  # noqa: F401

    assert rl_config.trustedHosts == []
    assert rl_config.trustedSchemes == []


# ---------------------------------------------------------------------------
# Template-string sandbox escape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "{name.__class__}",
        "{name.__class__.__mro__[1].__subclasses__}",
        "{0.__globals__}",
        "{name!r}",
        "{name:>99999999}",
    ],
)
def test_template_strings_cannot_walk_attributes_or_allocate(hostile: str) -> None:
    # str.format_map on a user-supplied string reaches module internals through
    # attribute access, and a width spec allocates unbounded padding.
    from label_sheet_generator.catalog import parse_template_document
    from label_sheet_generator.errors import TemplateError

    with pytest.raises(TemplateError):
        parse_template_document(
            {
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
                "elements": [{"type": "text", "x_mm": 1, "y_mm": 1, "template": hostile}],
            }
        )


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_is_absent_by_default(client: TestClient) -> None:
    # The old app used allow_origins=["*"] with allow_credentials=True.
    # Starlette substitutes the *echoed* origin in that configuration, so the
    # effective policy was any-origin-with-credentials.
    response = client.get("/api/livez", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_a_disallowed_origin_is_not_reflected(settings: Settings) -> None:
    configured = replace(settings, cors_origins=("https://good.example",))
    with TestClient(create_app(configured)) as client:
        allowed = client.get("/api/livez", headers={"Origin": "https://good.example"})
        assert allowed.headers.get("access-control-allow-origin") == "https://good.example"

        denied = client.get("/api/livez", headers={"Origin": "https://evil.example"})
        assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


def test_credentials_are_never_allowed(settings: Settings) -> None:
    configured = replace(settings, cors_origins=("https://good.example",))
    with TestClient(create_app(configured)) as client:
        response = client.get("/api/livez", headers={"Origin": "https://good.example"})
        assert response.headers.get("access-control-allow-credentials") != "true"


def test_a_wildcard_origin_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from label_sheet_generator.errors import ConfigurationError

    monkeypatch.setenv("LSG_CORS_ORIGINS", "*")
    with pytest.raises(ConfigurationError, match="explicit origins"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/livez", "/api/bootstrap", "/api/nope"])
def test_security_headers_are_on_every_response_including_errors(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_csp_forbids_inline_script_and_framing(client: TestClient) -> None:
    csp = client.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


def test_the_one_inline_script_is_allowed_by_hash_not_by_unsafe_inline(
    client: TestClient,
) -> None:
    # The theme bootstrap must run before first paint to avoid a flash, so it
    # cannot be an external file. Allowing it by hash keeps every *other*
    # inline script blocked.
    csp = client.get("/").headers["content-security-policy"]
    assert "'sha256-" in csp


# ---------------------------------------------------------------------------
# Content-Disposition header injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        'evil"; filename="owned.exe',
        "evil\r\nSet-Cookie: session=stolen",
        "evil\nX-Injected: 1",
        "../../etc/passwd",
        "a" * 5_000,
        "naïve résumé ‮gnp.exe",
        "; rm -rf /",
    ],
)
def test_a_hostile_filename_cannot_alter_the_header_structure(hostile: str) -> None:
    # The old code f-string-interpolated the user's filename straight into the
    # header value.
    header = content_disposition(hostile)
    assert header.count('filename="') == 1
    assert "\r" not in header
    assert "\n" not in header
    assert header.startswith("attachment; ")
    # The ASCII fallback must be a bare, quoted, single-segment name.
    ascii_name = header.split('filename="', 1)[1].split('"', 1)[0]
    assert "/" not in ascii_name
    assert ";" not in ascii_name
    assert len(ascii_name) <= 104


def test_filenames_always_end_in_pdf() -> None:
    assert sanitize_filename(None) == "labels.pdf"
    assert sanitize_filename("") == "labels.pdf"
    assert sanitize_filename("   ") == "labels.pdf"
    assert sanitize_filename("...") == "labels.pdf"
    assert sanitize_filename("report").endswith(".pdf")


def test_the_hostile_filename_survives_a_real_request(client: TestClient) -> None:
    response = client.post(
        "/api/render/pdf",
        json={
            "template_id": "avery:5160",
            "document": "",
            "filename": 'x"; filename="owned.exe',
        },
    )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    # The structure must be intact: exactly one filename parameter, and the
    # injected second one must not have become a parameter of its own.
    assert disposition.count('filename="') == 1
    ascii_name = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert ascii_name.endswith(".pdf")
    assert '"' not in ascii_name and ";" not in ascii_name


# ---------------------------------------------------------------------------
# Work and size limits
# ---------------------------------------------------------------------------


def test_an_oversized_body_is_refused_with_413(settings: Settings) -> None:
    small = replace(
        settings,
        max_body_bytes=4096,
        max_document_bytes=2048,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    with TestClient(create_app(small)) as client:
        response = client.post(
            "/api/render/plan",
            json={"template_id": "avery:5160", "document": "x" * 20_000},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


def test_too_many_records_is_a_422_naming_the_limit(settings: Settings) -> None:
    capped = replace(
        settings,
        max_records=10,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    document = json.dumps({"schema": ["name"], "records": [{"name": f"r{i}"} for i in range(50)]})
    with TestClient(create_app(capped)) as client:
        response = client.post(
            "/api/render/pdf", json={"template_id": "avery:5160", "document": document}
        )
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "limit_exceeded"
        assert body["limit_name"] == "max_records"


def test_too_many_pages_is_refused(settings: Settings) -> None:
    capped = replace(
        settings,
        max_pages=2,
        max_records=10_000,
        max_document_bytes=4_000_000,
        max_body_bytes=8_000_000,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    document = json.dumps({"schema": ["name"], "records": [{"name": f"r{i}"} for i in range(500)]})
    with TestClient(create_app(capped)) as client:
        response = client.post(
            "/api/render/pdf", json={"template_id": "avery:5160", "document": document}
        )
        assert response.status_code == 422
        assert response.json()["error"]["limit_name"] == "max_pages"


def test_an_oversized_upload_is_refused(settings: Settings) -> None:
    capped = replace(
        settings,
        max_upload_bytes=1024,
        max_body_bytes=4_000_000,
        max_document_bytes=1_000_000,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    payload = b"name\n" + b"".join(b"row-%d\n" % i for i in range(5_000))
    with TestClient(create_app(capped)) as client:
        response = client.post(
            "/api/records/parse",
            files={"file": ("big.csv", payload, "text/csv")},
            data={"fields": "[]"},
        )
        assert response.status_code == 413


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_the_render_limiter_trips_with_a_retry_after(settings: Settings) -> None:
    throttled = replace(settings, render_rate_limit_burst=3, render_rate_limit_per_second=0.01)
    with TestClient(create_app(throttled)) as client:
        statuses = [
            client.post(
                "/api/render/plan", json={"template_id": "avery:5160", "document": ""}
            ).status_code
            for _ in range(8)
        ]
        assert 429 in statuses, statuses
        limited = client.post(
            "/api/render/plan", json={"template_id": "avery:5160", "document": ""}
        )
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.json()["error"]["code"] == "rate_limited"


def test_health_probes_are_never_rate_limited(settings: Settings) -> None:
    # A platform probe throttled into failing would report a merely-busy
    # container as dead and get it restarted.
    throttled = replace(settings, rate_limit_burst=1, rate_limit_per_second=0.01)
    with TestClient(create_app(throttled)) as client:
        assert all(client.get("/api/livez").status_code == 200 for _ in range(20))


def test_forwarded_for_is_ignored_without_a_trusted_proxy(settings: Settings) -> None:
    # Otherwise a client spoofs a fresh address per request and the limiter is
    # decorative.
    throttled = replace(
        settings,
        trusted_proxy_hops=0,
        render_rate_limit_burst=3,
        render_rate_limit_per_second=0.01,
    )
    with TestClient(create_app(throttled)) as client:
        statuses = [
            client.post(
                "/api/render/plan",
                json={"template_id": "avery:5160", "document": ""},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            ).status_code
            for i in range(8)
        ]
        assert 429 in statuses, "spoofed X-Forwarded-For defeated the limiter"


# ---------------------------------------------------------------------------
# No user input may produce a 5xx, and nothing internal may leak
# ---------------------------------------------------------------------------


HOSTILE_BODIES: list[dict] = [
    {"template_id": "avery:5160", "overrides": {"margin_top_mm": float("nan")}},
    {"template_id": "avery:5160", "overrides": {"margin_top_mm": float("inf")}},
    {"template_id": "avery:5160", "overrides": {"margin_top_mm": -1}},
    {"template_id": "avery:5160", "overrides": {"margin_top_mm": 10**9}},
    {"template_id": "../../etc/passwd"},
    {"template_id": "/proc/self/environ"},
    {"template_id": "avery:5160", "document": "{not json"},
    {"template_id": "avery:5160", "document": '{"records": "not a list"}'},
    {"template_id": "avery:5160", "document": '{"records": [1, 2, 3]}'},
    {"template_id": "avery:5160", "document": '{"records": [{"a": {"b": 1}}]}'},
    {"template_id": "avery:5160", "document": '{"records": [], "schema": "x"}'},
    {"template_id": "avery:5160", "page_rotation_deg": 45},
    {"template_id": "avery:5160", "page_orientation": "sideways"},
    {"template_id": "avery:5160", "text_rotation_deg": float("nan")},
    {"template_id": "avery:5160", "unknown_field": 1},
    {"template_id": ""},
    {"template_id": "a" * 10_000},
    {},
]


@pytest.mark.parametrize("body", HOSTILE_BODIES, ids=range(len(HOSTILE_BODIES)))
@pytest.mark.parametrize("endpoint", ["/api/render/plan", "/api/render/pdf"])
def test_no_hostile_body_produces_a_5xx(client: TestClient, endpoint: str, body: dict) -> None:
    # NaN margins returned a 500 from the old app, because every comparison
    # against NaN in the bounds checks was False and nothing ever raised.
    response = client.post(
        endpoint, content=json.dumps(body), headers={"content-type": "application/json"}
    )
    assert response.status_code < 500, (
        f"{endpoint} returned {response.status_code} for {body}: {response.text[:300]}"
    )


@pytest.mark.parametrize("body", HOSTILE_BODIES, ids=range(len(HOSTILE_BODIES)))
def test_error_bodies_never_leak_internals(client: TestClient, body: dict) -> None:
    response = client.post(
        "/api/render/pdf", content=json.dumps(body), headers={"content-type": "application/json"}
    )
    if response.status_code == 200:
        return
    text = response.text
    for leak in ("Traceback", "/root/", "/usr/lib/", "site-packages", 'File "'):
        assert leak not in text, f"response leaked {leak!r}: {text[:300]}"
    error = response.json()["error"]
    assert set(error) >= {"code", "message", "request_id", "details"}


def test_a_request_id_is_always_present_and_echoed(client: TestClient) -> None:
    minted = client.get("/api/livez")
    assert minted.headers["x-request-id"]

    echoed = client.get("/api/livez", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"


def test_a_hostile_request_id_is_not_reflected(client: TestClient) -> None:
    # It lands in a response header and in the logs, so it has to be bounded
    # and free of control characters.
    response = client.get("/api/livez", headers={"X-Request-ID": "a" * 500})
    assert len(response.headers["x-request-id"]) <= 64


# ---------------------------------------------------------------------------
# Frontend XSS discipline
# ---------------------------------------------------------------------------


STATIC = Path(__file__).resolve().parents[1] / "src/label_sheet_generator/static"


def _strip_js_comments(source: str) -> str:
    """Drop comments so a rule *documented* in prose is not read as a violation."""
    import re

    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


def test_the_frontend_never_assigns_html() -> None:
    # Template names, field names, record values and server error strings are
    # all untrusted and all reach the DOM.
    import re

    source = _strip_js_comments((STATIC / "app.js").read_text())
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert not re.search(rf"\b{banned}\b", source), f"app.js uses {banned}"


def test_the_frontend_never_evaluates_strings() -> None:
    source = _strip_js_comments((STATIC / "app.js").read_text())
    assert "eval(" not in source
    assert "new Function(" not in source


def test_the_frontend_has_no_inline_event_handlers() -> None:
    import re

    html = (STATIC / "index.html").read_text()
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html), "inline handler in index.html"


def test_the_frontend_loads_nothing_from_the_network() -> None:
    # It must work offline, and a CDN is a third party that can change the code
    # running against the user's data.
    import re

    for name in ("index.html", "app.css", "app.js"):
        text = (STATIC / name).read_text()
        assert not re.search(r"https?://(cdn|unpkg|jsdelivr|fonts\.googleapis)", text), name
