"""Runtime configuration, resolved once at startup.

Every root directory and every limit lives here. Two rules:

* **No ``.`` defaults.** The old code defaulted ``base_dir`` to the current
  working directory throughout, so the set of available templates depended on
  where the process happened to be started -- fine for a CLI run from a
  checkout, silently empty in a container. Built-in data is located through
  :mod:`importlib.resources` instead, so it works from a wheel.
* **Limits are named constants, not magic numbers.** Each one is reported by
  ``/api/bootstrap`` so the frontend can enforce the same bound before a
  request is sent, and each appears by name in the 4xx it produces.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from label_sheet_generator.errors import ConfigurationError


def _package_data(*parts: str) -> Path:
    return Path(str(resources.files("label_sheet_generator").joinpath(*parts)))


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw and raw.strip() else default


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime configuration. Build with :meth:`from_env`."""

    # --- Roots ------------------------------------------------------------
    #: Built-in templates shipped inside the wheel.
    builtin_template_root: Path
    #: Built-in example record documents.
    example_root: Path
    #: Optional directory of user templates layered over the built-ins.
    user_template_root: Path | None
    #: The only directory from which label images may be loaded.
    asset_root: Path | None

    # --- Limits -----------------------------------------------------------
    max_body_bytes: int = 2 * 1024 * 1024
    max_upload_bytes: int = 2 * 1024 * 1024
    max_document_bytes: int = 1024 * 1024
    max_template_bytes: int = 512 * 1024
    max_records: int = 5_000
    max_pages: int = 200
    max_output_bytes: int = 32 * 1024 * 1024
    max_asset_bytes: int = 4 * 1024 * 1024
    max_asset_pixels: int = 40_000_000
    max_field_value_length: int = 4_000
    render_timeout_s: float = 20.0

    # --- Concurrency and rate limiting ------------------------------------
    max_concurrent_renders: int = 4
    rate_limit_burst: int = 40
    rate_limit_per_second: float = 4.0
    render_rate_limit_burst: int = 12
    render_rate_limit_per_second: float = 1.5
    trusted_proxy_hops: int = 0

    # --- Preview ----------------------------------------------------------
    preview_scale_min: float = 0.5
    preview_scale_max: float = 3.0
    preview_scale_default: float = 1.5

    # --- HTTP -------------------------------------------------------------
    cors_origins: tuple[str, ...] = ()
    enable_pdf_import: bool = False
    enable_catalog_reload: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``LSG_*`` environment variables."""
        origins = tuple(_env_list("LSG_CORS_ORIGINS"))
        if "*" in origins:
            raise ConfigurationError(
                "LSG_CORS_ORIGINS must list explicit origins; '*' is refused because "
                "a wildcard origin cannot be combined safely with credentials"
            )

        user_root = os.environ.get("LSG_TEMPLATE_DIR", "").strip()
        asset_root = os.environ.get("LSG_ASSET_DIR", "").strip()

        return cls(
            builtin_template_root=_package_data("data", "templates"),
            example_root=_package_data("data", "examples"),
            user_template_root=Path(user_root).expanduser() if user_root else None,
            asset_root=Path(asset_root).expanduser() if asset_root else None,
            max_body_bytes=_env_int("LSG_MAX_BODY_BYTES", 2 * 1024 * 1024, minimum=1024),
            max_upload_bytes=_env_int("LSG_MAX_UPLOAD_BYTES", 2 * 1024 * 1024, minimum=1024),
            max_document_bytes=_env_int("LSG_MAX_DOCUMENT_BYTES", 1024 * 1024, minimum=1024),
            max_records=_env_int("LSG_MAX_RECORDS", 5_000),
            max_pages=_env_int("LSG_MAX_PAGES", 200),
            max_output_bytes=_env_int("LSG_MAX_OUTPUT_BYTES", 32 * 1024 * 1024, minimum=1024),
            render_timeout_s=_env_float("LSG_RENDER_TIMEOUT_S", 20.0, minimum=0.1),
            max_concurrent_renders=_env_int("LSG_MAX_CONCURRENT_RENDERS", 4),
            rate_limit_burst=_env_int("LSG_RATE_LIMIT_BURST", 40),
            rate_limit_per_second=_env_float("LSG_RATE_LIMIT_PER_SECOND", 4.0, minimum=0.01),
            render_rate_limit_burst=_env_int("LSG_RENDER_RATE_LIMIT_BURST", 12),
            render_rate_limit_per_second=_env_float(
                "LSG_RENDER_RATE_LIMIT_PER_SECOND", 1.5, minimum=0.01
            ),
            trusted_proxy_hops=_env_int("LSG_TRUSTED_PROXY_HOPS", 0, minimum=0),
            cors_origins=origins,
            enable_pdf_import=_env_bool("LSG_ENABLE_PDF_IMPORT", False),
            enable_catalog_reload=_env_bool("LSG_ENABLE_CATALOG_RELOAD", False),
            host=_env_str("LSG_HOST", "127.0.0.1"),
            port=_env_int("PORT", _env_int("LSG_PORT", 8000)),
            log_level=_env_str("LSG_LOG_LEVEL", "INFO").upper(),
        )

    def validate(self) -> None:
        """Fail fast on a misconfigured server. Called at startup only."""
        if not self.builtin_template_root.is_dir():
            raise ConfigurationError(
                f"built-in template directory is missing at {self.builtin_template_root}; "
                "the package data did not ship correctly"
            )
        if self.user_template_root is not None and not self.user_template_root.is_dir():
            raise ConfigurationError(
                f"LSG_TEMPLATE_DIR points at {self.user_template_root}, which is not a directory"
            )
        if self.asset_root is not None and not self.asset_root.is_dir():
            raise ConfigurationError(
                f"LSG_ASSET_DIR points at {self.asset_root}, which is not a directory"
            )
        if self.max_document_bytes > self.max_body_bytes:
            raise ConfigurationError("LSG_MAX_DOCUMENT_BYTES cannot exceed LSG_MAX_BODY_BYTES")

    def public_limits(self) -> dict[str, float | int]:
        """The subset of limits the frontend needs in order to pre-validate."""
        return {
            "max_body_bytes": self.max_body_bytes,
            "max_upload_bytes": self.max_upload_bytes,
            "max_document_bytes": self.max_document_bytes,
            "max_records": self.max_records,
            "max_pages": self.max_pages,
            "max_field_value_length": self.max_field_value_length,
            "preview_scale_min": self.preview_scale_min,
            "preview_scale_max": self.preview_scale_max,
            "preview_scale_default": self.preview_scale_default,
        }


def default_settings() -> Settings:
    """Settings for library and CLI use, where no server is running."""
    return Settings(
        builtin_template_root=_package_data("data", "templates"),
        example_root=_package_data("data", "examples"),
        user_template_root=None,
        asset_root=None,
    )
