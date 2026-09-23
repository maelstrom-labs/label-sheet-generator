"""Environment parsing, validation, and the startup fail-fast contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from label_sheet_generator.errors import ConfigurationError
from label_sheet_generator.settings import Settings, default_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a known-empty environment."""
    for key in list(__import__("os").environ):
        if key.startswith("LSG_") or key == "PORT":
            monkeypatch.delenv(key, raising=False)


def test_defaults_resolve_to_packaged_data() -> None:
    settings = Settings.from_env()
    settings.validate()
    assert settings.builtin_template_root.is_dir()
    assert (settings.builtin_template_root / "labels").is_dir()
    assert settings.user_template_root is None
    # Images off unless a directory is configured: record data is user input,
    # so an unrestricted image path would be a file-read primitive.
    assert settings.asset_root is None


def test_no_setting_defaults_to_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    # The old code defaulted base_dir to "." everywhere, so the template list
    # depended on where the process was started -- empty in a container.
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    settings.validate()
    assert settings.builtin_template_root.is_absolute()
    assert tmp_path not in settings.builtin_template_root.parents


# --- CORS -----------------------------------------------------------------


def test_a_wildcard_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_CORS_ORIGINS", "*")
    with pytest.raises(ConfigurationError, match="explicit origins"):
        Settings.from_env()


def test_a_wildcard_among_real_origins_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LSG_CORS_ORIGINS", "https://a.example,*")
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_origins_are_split_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_CORS_ORIGINS", " https://a.example , https://b.example ,, ")
    assert Settings.from_env().cors_origins == ("https://a.example", "https://b.example")


def test_no_origins_means_no_cors() -> None:
    assert Settings.from_env().cors_origins == ()


# --- Numeric parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "name,attribute",
    [
        ("LSG_MAX_RECORDS", "max_records"),
        ("LSG_MAX_PAGES", "max_pages"),
        ("LSG_MAX_BODY_BYTES", "max_body_bytes"),
        ("LSG_MAX_UPLOAD_BYTES", "max_upload_bytes"),
        ("LSG_MAX_CONCURRENT_RENDERS", "max_concurrent_renders"),
    ],
)
def test_integer_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, name: str, attribute: str
) -> None:
    monkeypatch.setenv(name, "4242")
    assert getattr(Settings.from_env(), attribute) == 4242


def test_a_non_numeric_value_fails_at_startup_not_at_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LSG_MAX_RECORDS", "lots")
    with pytest.raises(ConfigurationError, match="must be an integer"):
        Settings.from_env()


def test_a_value_below_the_floor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_MAX_RECORDS", "0")
    with pytest.raises(ConfigurationError, match="at least"):
        Settings.from_env()


def test_a_non_numeric_float_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_RENDER_TIMEOUT_S", "soon")
    with pytest.raises(ConfigurationError, match="must be a number"):
        Settings.from_env()


def test_blank_values_fall_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_MAX_RECORDS", "   ")
    assert Settings.from_env().max_records == default_settings().max_records


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_boolean_settings_accept_the_usual_spellings(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("LSG_ENABLE_PDF_IMPORT", truthy)
    assert Settings.from_env().enable_pdf_import is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "anything-else"])
def test_anything_else_is_false(monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
    monkeypatch.setenv("LSG_ENABLE_PDF_IMPORT", falsy)
    assert Settings.from_env().enable_pdf_import is False


def test_pdf_import_is_off_by_default() -> None:
    # It parses untrusted PDFs, which is a far larger attack surface than JSON.
    assert Settings.from_env().enable_pdf_import is False


def test_port_prefers_the_platform_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Render, Fly, Koyeb and Cloud Run all inject $PORT.
    monkeypatch.setenv("LSG_PORT", "9000")
    monkeypatch.setenv("PORT", "10000")
    assert Settings.from_env().port == 10000


def test_lsg_port_is_used_when_platform_port_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LSG_PORT", "9000")
    assert Settings.from_env().port == 9000


# --- Directory validation -------------------------------------------------


def test_a_missing_template_dir_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_TEMPLATE_DIR", "/definitely/not/here")
    with pytest.raises(ConfigurationError, match="not a directory"):
        Settings.from_env().validate()


def test_a_missing_asset_dir_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LSG_ASSET_DIR", "/definitely/not/here")
    with pytest.raises(ConfigurationError, match="not a directory"):
        Settings.from_env().validate()


def test_a_real_template_dir_validates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LSG_TEMPLATE_DIR", str(tmp_path))
    settings = Settings.from_env()
    settings.validate()
    assert settings.user_template_root == tmp_path


def test_a_document_cap_above_the_body_cap_is_incoherent() -> None:
    settings = replace(default_settings(), max_document_bytes=10, max_body_bytes=5)
    with pytest.raises(ConfigurationError, match="cannot exceed"):
        settings.validate()


# --- Public surface -------------------------------------------------------


def test_public_limits_expose_what_the_frontend_pre_validates() -> None:
    limits = default_settings().public_limits()
    assert {
        "max_body_bytes",
        "max_upload_bytes",
        "max_document_bytes",
        "max_records",
        "max_pages",
        "preview_scale_min",
        "preview_scale_max",
        "preview_scale_default",
    } <= set(limits)
    assert all(isinstance(value, (int, float)) for value in limits.values())


def test_settings_are_immutable() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        default_settings().max_records = 1  # type: ignore[misc]
