"""Response security headers and the Content-Security-Policy.

The app serves its own HTML and JavaScript, which is exactly when these stop
being optional. The policy is deliberately strict because the frontend needs
nothing it forbids: no CDN, no inline handlers, no ``eval``, no framing.

One inline ``<script>`` is unavoidable -- the theme bootstrap that must run
before first paint to avoid a flash of the wrong colour scheme. It is allowed
by its SHA-256 hash rather than by ``unsafe-inline``, so any *other* inline
script is still blocked.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)


def script_hashes(html: str) -> list[str]:
    """CSP ``sha256-`` sources for every inline script in ``html``."""
    hashes = []
    for match in _INLINE_SCRIPT_RE.finditer(html):
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return hashes


def build_csp(extra_script_sources: list[str] | None = None) -> str:
    """Assemble the policy. ``style-src`` includes the hash-free 'self' only."""
    script_src = ["'self'", *(extra_script_sources or [])]
    directives = [
        "default-src 'self'",
        f"script-src {' '.join(script_src)}",
        "style-src 'self'",
        "img-src 'self' blob: data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
    return "; ".join(directives)


class SecurityHeadersMiddleware:
    """Attach the security headers to every response, including errors."""

    def __init__(self, app: ASGIApp, *, csp: str, hsts: bool = False) -> None:
        self.app = app
        self.csp = csp
        self.hsts = hsts

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(
                    (key.encode("latin-1"), value.encode("latin-1"))
                    for key, value in self._headers()
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _headers(self) -> list[tuple[str, str]]:
        headers = [
            ("content-security-policy", self.csp),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", "no-referrer"),
            ("x-frame-options", "DENY"),
            ("permissions-policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"),
            ("cross-origin-opener-policy", "same-origin"),
        ]
        if self.hsts:
            headers.append(("strict-transport-security", "max-age=31536000; includeSubDomains"))
        return headers


async def add_request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Propagate an inbound ``X-Request-ID`` or mint one.

    The value is echoed in every error body so a user can quote it in a bug
    report without the server ever returning a stack trace.
    """
    import uuid  # noqa: PLC0415

    inbound = request.headers.get("x-request-id", "")
    # Bound and sanitise: this value goes into a response header and logs.
    request_id = inbound if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", inbound) else uuid.uuid4().hex
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response
