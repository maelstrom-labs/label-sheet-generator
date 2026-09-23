"""Mapping the domain exception hierarchy onto HTTP.

Two invariants this module exists to hold:

* **No user-supplied input may produce a 5xx.** Every predictable failure has a
  4xx with a stable ``code`` the frontend can branch on. A 500 here means a
  genuine bug, and is the only case that gets logged with a traceback.
* **Nothing internal leaks.** The catch-all returns a fixed message plus the
  request id. It never returns ``str(exc)``, an absolute path, or a stack
  trace -- the previous app returned all three.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from label_sheet_generator.errors import (
    AssetError,
    ConfigurationError,
    LabelSheetError,
    LimitExceeded,
    NotFoundError,
    Overloaded,
    PayloadTooLarge,
    RateLimited,
    RenderTimeout,
    ValidationError,
)

logger = logging.getLogger("label_sheet_generator.api")

#: At or above this status the failure is ours, not the caller's, so it is
#: logged with a traceback rather than merely returned.
SERVER_ERROR_THRESHOLD = 500

#: Domain error -> HTTP status. Anything not listed falls back to 422, which is
#: the right default because every remaining domain error describes bad input.
_STATUS_BY_TYPE: tuple[tuple[type[LabelSheetError], int], ...] = (
    (PayloadTooLarge, 413),
    (RateLimited, 429),
    (Overloaded, 503),
    (RenderTimeout, 503),
    (NotFoundError, 404),
    (LimitExceeded, 422),
    (AssetError, 422),
    (ValidationError, 422),
    (ConfigurationError, 500),
)


def status_for(exc: LabelSheetError) -> int:
    for error_type, status in _STATUS_BY_TYPE:
        if isinstance(exc, error_type):
            return status
    return 422


def error_body(
    code: str,
    message: str,
    *,
    request_id: str,
    details: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "details": details or [],
    }
    if extra:
        payload.update(extra)
    return {"error": payload}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _retry_headers(exc: LabelSheetError) -> dict[str, str]:
    retry_after = getattr(exc, "retry_after", None)
    return {"Retry-After": str(int(retry_after))} if retry_after else {}


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler. Call once, from the app factory."""

    @app.exception_handler(LabelSheetError)
    async def _domain(request: Request, exc: LabelSheetError) -> JSONResponse:
        status = status_for(exc)
        extra = {
            key: value
            for key, value in exc.to_dict().items()
            if key in {"limit_name", "limit", "actual", "loc", "record_index", "element_index"}
        }
        if status >= SERVER_ERROR_THRESHOLD:
            logger.exception("configuration or internal domain error", exc_info=exc)
        return JSONResponse(
            status_code=status,
            content=error_body(
                exc.code,
                exc.message,
                request_id=_request_id(request),
                details=exc.details,
                extra=extra,
            ),
            headers=_retry_headers(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error",
                "the request body is not valid",
                request_id=_request_id(request),
                details=_pydantic_details(exc.errors()),
            ),
        )

    @app.exception_handler(PydanticValidationError)
    async def _pydantic(request: Request, exc: PydanticValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error",
                "the supplied document is not valid",
                request_id=_request_id(request),
                details=_pydantic_details(exc.errors()),
            ),
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                _CODE_BY_STATUS.get(exc.status_code, "http_error"),
                str(exc.detail),
                request_id=_request_id(request),
            ),
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        # The traceback goes to the log, never to the client.
        logger.exception("unhandled error on %s (request_id=%s)", request.url.path, request_id)
        return JSONResponse(
            status_code=500,
            content=error_body(
                "internal_error",
                "something went wrong on the server; quote the request id if you report this",
                request_id=request_id,
            ),
        )


_CODE_BY_STATUS = {
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "unavailable",
}


def _pydantic_details(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Reduce pydantic errors to a stable, leak-free shape.

    ``ctx`` and ``input`` are dropped: they can echo the submitted value, which
    may be large, and ``url`` points at pydantic's docs rather than anything
    the caller of this API can act on.
    """
    details = []
    for error in errors[:20]:
        details.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "invalid")),
                "type": str(error.get("type", "value_error")),
            }
        )
    return details
