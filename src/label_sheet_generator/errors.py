"""Domain exception hierarchy.

Every failure the library can produce is one of these. They carry a stable
machine-readable ``code``, a client-safe ``message``, and an optional ``loc``
pointing at the offending part of the input. Nothing below the API layer knows
about HTTP; :mod:`label_sheet_generator.api.errors` does the mapping.

The invariant this file exists to enforce: no user-supplied input may ever
surface as an untyped exception. A bare ``TypeError`` escaping the parse layer
is a bug, because at the HTTP boundary it becomes a 500.
"""

from __future__ import annotations

from typing import Any

Loc = tuple[str | int, ...]


class LabelSheetError(Exception):
    """Base class for every error this package raises deliberately."""

    code = "error"

    def __init__(
        self,
        message: str,
        *,
        loc: Loc = (),
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.loc = loc
        self.details = details or []

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.loc:
            payload["loc"] = list(self.loc)
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        if self.loc:
            return f"{_format_loc(self.loc)}: {self.message}"
        return self.message


class ValidationError(LabelSheetError):
    """Input is structurally or semantically invalid. Maps to HTTP 422."""

    code = "validation_error"


class TemplateError(ValidationError):
    """A template document is invalid.

    Kept as a distinct name because it is the historical public exception of
    this package and callers catch it.
    """

    code = "template_error"


class RecordError(ValidationError):
    """A record document is invalid."""

    code = "record_error"


class GeometryError(ValidationError):
    """A grid or element box does not fit where it claims to."""

    code = "geometry_error"


class NotFoundError(LabelSheetError):
    """A named template, layout, preset, or asset does not exist. HTTP 404."""

    code = "not_found"


class LimitExceeded(LabelSheetError):
    """A configured limit was exceeded. HTTP 422 (or 413 for raw body size).

    ``limit`` and ``actual`` are always reported so the caller can tell the user
    what to change rather than just that something was too big.
    """

    code = "limit_exceeded"

    def __init__(
        self,
        message: str,
        *,
        limit_name: str,
        limit: float,
        actual: float | None = None,
        loc: Loc = (),
    ) -> None:
        super().__init__(message, loc=loc)
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["limit_name"] = self.limit_name
        payload["limit"] = self.limit
        if self.actual is not None:
            payload["actual"] = self.actual
        return payload


class PayloadTooLarge(LimitExceeded):
    """The raw request body exceeded its cap. HTTP 413."""

    code = "payload_too_large"


class AssetError(LabelSheetError):
    """An image or font reference could not be resolved safely.

    Deliberately never echoes the resolved filesystem path: the message is what
    the client sees, and this class is the one most likely to be probed.
    """

    code = "asset_error"


class UnsafePathError(AssetError):
    """A path escaped its sandbox root, or used a rejected scheme or segment."""

    code = "unsafe_path"


class RenderError(LabelSheetError):
    """Rendering failed for a reason attributable to the input."""

    code = "render_error"

    def __init__(
        self,
        message: str,
        *,
        record_index: int | None = None,
        element_index: int | None = None,
        loc: Loc = (),
    ) -> None:
        super().__init__(message, loc=loc)
        self.record_index = record_index
        self.element_index = element_index

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.record_index is not None:
            payload["record_index"] = self.record_index
        if self.element_index is not None:
            payload["element_index"] = self.element_index
        return payload

    def __str__(self) -> str:
        where = []
        if self.record_index is not None:
            where.append(f"record {self.record_index + 1}")
        if self.element_index is not None:
            where.append(f"element {self.element_index}")
        if where:
            return f"{', '.join(where)}: {self.message}"
        return super().__str__()


class RenderTimeout(RenderError):
    """A render exceeded its wall-clock budget. HTTP 503."""

    code = "render_timeout"


class Overloaded(LabelSheetError):
    """The render concurrency limit is saturated. HTTP 503 + Retry-After."""

    code = "overloaded"

    def __init__(
        self, message: str = "server is busy; retry shortly", *, retry_after: int = 5
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(LabelSheetError):
    """Too many requests from one client. HTTP 429 + Retry-After."""

    code = "rate_limited"

    def __init__(self, message: str = "too many requests", *, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ConfigurationError(LabelSheetError):
    """The server is misconfigured. Raised at startup only, never per request."""

    code = "configuration_error"


def _format_loc(loc: Loc) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = part
    return out
