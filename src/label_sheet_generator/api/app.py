"""FastAPI application factory.

The app serves both the JSON API under ``/api`` and the single-page frontend
from ``/``, in one process. That is what makes this deployable on a free tier
as a single container with no Node build step and no second service.

Middleware order matters and is asserted by the layout below. Outermost first:

1. security headers -- must wrap everything, including responses produced by
   middleware that rejects a request before it reaches a route;
2. body-size limit -- must run before anything buffers the body;
3. rate limiting -- cheap, and should reject before real work starts;
4. request id -- needs to be inside the handlers so error bodies can quote it;
5. CORS, only when explicitly configured.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp

from label_sheet_generator import __version__
from label_sheet_generator.api.errors import error_body, register_exception_handlers
from label_sheet_generator.api.limits import (
    BodySizeLimitMiddleware,
    TokenBucketLimiter,
    client_key,
)
from label_sheet_generator.api.routes import router
from label_sheet_generator.api.security import (
    SecurityHeadersMiddleware,
    add_request_id,
    build_csp,
    script_hashes,
)
from label_sheet_generator.catalog import Catalog
from label_sheet_generator.logging_config import configure as configure_logging
from label_sheet_generator.service import LabelSheetService
from label_sheet_generator.settings import Settings

logger = logging.getLogger("label_sheet_generator.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. One call per process."""
    resolved = settings or Settings.from_env()
    resolved.validate()
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        catalog = Catalog.build(resolved)
        app.state.settings = resolved
        app.state.service = LabelSheetService(resolved, catalog)
        logger.info(
            "catalog built",
            extra={
                "templates": len(catalog.label_templates()),
                "layouts": len(catalog.layout_templates()),
                "broken": len(catalog.broken),
            },
        )
        for broken in catalog.broken:
            logger.warning("template failed to load", extra=broken.to_dict())
        try:
            yield
        finally:
            app.state.service.close()

    app = FastAPI(
        title="Label Sheet Generator",
        version=__version__,
        description="Generate print-ready label sheet PDFs from JSON templates and records.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    index_html = STATIC_DIR / "index.html"
    csp = build_csp(script_hashes(index_html.read_text("utf-8")) if index_html.is_file() else [])

    register_exception_handlers(app)
    app.include_router(router, prefix="/api")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> Response:
            if not index_html.is_file():  # pragma: no cover - packaging failure
                return JSONResponse(
                    status_code=503,
                    content=error_body(
                        "frontend_missing",
                        "the frontend assets are not installed",
                        request_id="startup",
                    ),
                )
            return FileResponse(index_html, headers={"Cache-Control": "no-cache"})

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> Response:
            icon = STATIC_DIR / "favicon.svg"
            if icon.is_file():
                return FileResponse(icon, media_type="image/svg+xml")
            return Response(status_code=404)

    # --- Middleware, registered innermost-first -------------------------

    if resolved.cors_origins:
        # Only ever an explicit allow-list. A wildcard is refused in Settings,
        # because Starlette echoes the request Origin when a wildcard is
        # combined with credentials, which is the opposite of a restriction.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["content-type", "if-none-match", "x-request-id"],
            max_age=600,
        )

    app.add_middleware(BaseHTTPMiddleware, dispatch=add_request_id)
    app.add_middleware(
        RateLimitMiddleware,
        general=TokenBucketLimiter(
            rate_per_second=resolved.rate_limit_per_second, burst=resolved.rate_limit_burst
        ),
        render=TokenBucketLimiter(
            rate_per_second=resolved.render_rate_limit_per_second,
            burst=resolved.render_rate_limit_burst,
        ),
        trusted_proxy_hops=resolved.trusted_proxy_hops,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=resolved.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, csp=csp)

    return app


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Two buckets: a general one, and a tighter one for render endpoints.

    Rendering is orders of magnitude more expensive than a catalog lookup, so
    one shared budget would either throttle browsing or fail to protect the
    renderer.
    """

    #: Paths exempt from limiting entirely, so a platform health probe cannot
    #: be rate-limited into reporting the container dead.
    EXEMPT = frozenset({"/api/livez", "/api/readyz"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        general: TokenBucketLimiter,
        render: TokenBucketLimiter,
        trusted_proxy_hops: int,
    ) -> None:
        super().__init__(app)
        self.general = general
        self.render = render
        self.trusted_proxy_hops = trusted_proxy_hops

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path in self.EXEMPT or not path.startswith("/api/"):
            return await call_next(request)

        key = client_key(request.scope, trusted_proxy_hops=self.trusted_proxy_hops)
        limiter = self.render if path.startswith("/api/render/") else self.general
        allowed, retry_after = limiter.check(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content=error_body(
                    "rate_limited",
                    "too many requests; slow down and try again shortly",
                    request_id=getattr(request.state, "request_id", "unknown"),
                ),
                headers={"Retry-After": str(int(retry_after) or 1)},
            )
        return await call_next(request)


def run() -> int:
    """``label-sheet serve`` entry point."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print(
            "error: web dependencies are not installed. Install them with "
            "`pip install 'label-sheet-generator[web]'`."
        )
        return 2

    settings = Settings.from_env()
    uvicorn.run(
        "label_sheet_generator.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trusted_proxy_hops > 0,
        forwarded_allow_ips="*" if settings.trusted_proxy_hops > 0 else None,
        log_config=None,
    )
    return 0
