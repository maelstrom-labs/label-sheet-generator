"""HTTP routes.

Handlers are deliberately thin and contain no ``try``/``except``: every
failure is a domain exception that the handlers in
:mod:`label_sheet_generator.api.errors` already map to the right status. A
``try`` here would be a second, divergent error policy.

CPU-bound work runs through ``run_in_threadpool`` so a render never occupies
the event loop, and uploads are read in bounded chunks rather than with a
single unbounded ``await file.read()``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from label_sheet_generator import __version__
from label_sheet_generator.api.models import (
    PreviewRequest,
    RenderRequest,
    ValidateRecordsRequest,
    ValidateTemplateRequest,
)
from label_sheet_generator.avery import build_template, iter_presets
from label_sheet_generator.catalog import parse_template_document
from label_sheet_generator.errors import (
    LimitExceeded,
    NotFoundError,
    PayloadTooLarge,
    ValidationError,
)
from label_sheet_generator.fonts import available_fonts
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.records import analyze, format_document, parse_document, parse_upload
from label_sheet_generator.render import RenderOptions
from label_sheet_generator.schema import LabelTemplate
from label_sheet_generator.service import LabelSheetService, MarginOverrides

router = APIRouter()

#: Chunk size for streaming an upload. Small enough that an oversized file is
#: refused after ~64KB rather than after the whole body is resident.
_UPLOAD_CHUNK = 64 * 1024

#: Longest plausible Avery-style product code, e.g. "L7160". Bounds the value
#: before it reaches the preset lookup or any error message.
MAX_PRESET_CODE_LENGTH = 16


def _static_dir() -> Path:
    """Where the frontend lives, relative to this package rather than the CWD."""
    return Path(__file__).resolve().parent.parent / "static"


def get_service(request: Request) -> LabelSheetService:
    return request.app.state.service  # type: ignore[no-any-return]


Service = Annotated[LabelSheetService, Depends(get_service)]


def _options(payload: RenderRequest) -> RenderOptions:
    return RenderOptions(
        page_orientation=payload.page_orientation,
        page_rotation_deg=payload.page_rotation_deg,
        text_rotation_deg=payload.text_rotation_deg,
        outline_slots=payload.outline_slots,
        bleed_guide_inset_mm=payload.bleed_guide_inset_mm,
    )


def _overrides(payload: RenderRequest) -> MarginOverrides:
    return MarginOverrides(
        top_mm=payload.overrides.margin_top_mm,
        right_mm=payload.overrides.margin_right_mm,
        bottom_mm=payload.overrides.margin_bottom_mm,
        left_mm=payload.overrides.margin_left_mm,
    )


def _check_document_size(document: str, service: LabelSheetService) -> None:
    size = len(document.encode("utf-8"))
    if size > service.settings.max_document_bytes:
        raise LimitExceeded(
            f"the record document is {size} bytes; the maximum is "
            f"{service.settings.max_document_bytes}",
            limit_name="max_document_bytes",
            limit=service.settings.max_document_bytes,
            actual=size,
            loc=("document",),
        )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@router.get("/livez", tags=["ops"])
async def livez() -> dict[str, str]:
    """Always 200 while the process is alive.

    ``async`` with no I/O on purpose: it answers from the event loop even when
    every render worker is busy, so a liveness probe cannot be starved into
    killing a merely-loaded container.
    """
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
async def readyz(service: Service, response: Response) -> dict[str, Any]:
    """Per-dependency readiness, answered entirely from startup state."""
    checks = {
        "catalog_loaded": len(service.catalog) > 0,
        "label_templates": bool(service.catalog.label_templates()),
        "frontend_assets": _static_dir().is_dir(),
    }
    ready = all(checks.values())
    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@router.get("/version", tags=["ops"])
async def version() -> dict[str, str]:
    return {
        "version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/bootstrap", tags=["catalog"])
async def bootstrap(service: Service) -> dict[str, Any]:
    """Everything the frontend needs to render its first frame."""
    payload = service.bootstrap()
    payload["fonts"] = available_fonts()
    payload["version"] = {"version": __version__}
    return payload


@router.get("/templates", tags=["catalog"])
async def list_templates(service: Service, kind: str | None = None) -> dict[str, Any]:
    entries = [*service.catalog.label_templates(), *service.catalog.layout_templates()]
    if kind is not None:
        if kind not in {"label", "text-layout"}:
            raise ValidationError("kind must be 'label' or 'text-layout'", loc=("kind",))
        entries = [entry for entry in entries if entry.kind == kind]
    return {"templates": [entry.to_dict() for entry in entries]}


@router.get("/templates/{entry_id:path}", tags=["catalog"])
async def get_template(entry_id: str, service: Service) -> dict[str, Any]:
    entry = service.catalog.get(entry_id)
    return {
        "template": entry.template.dump(),
        "fields": list(entry.fields),
        "units": entry.template.units,
        "geometry": entry.geometry,
        "name": entry.name,
        "kind": entry.kind,
    }


@router.post("/templates/validate", tags=["catalog"])
async def validate_template(payload: ValidateTemplateRequest, service: Service) -> dict[str, Any]:
    """Validate a template document without saving or rendering it."""
    template = parse_template_document(payload.template)
    report = validate_geometry(template).to_dict() if isinstance(template, LabelTemplate) else None
    return {
        "ok": True,
        "normalized": template.dump(),
        "units": template.units,
        "fields": template.field_names,
        "geometry": report,
    }


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@router.post("/records/parse", tags=["records"])
async def parse_records(
    service: Service,
    file: Annotated[UploadFile, File()],
    fields: Annotated[str, Form()] = "[]",
) -> dict[str, Any]:
    """Convert an uploaded CSV/TSV/JSON file into the canonical document."""
    try:
        parsed_fields = json.loads(fields)
    except ValueError as exc:
        raise ValidationError("fields must be a JSON array", loc=("fields",)) from exc
    if not isinstance(parsed_fields, list):
        raise ValidationError("fields must be a JSON array", loc=("fields",))

    data = await _read_upload(file, service.settings.max_upload_bytes)
    report = await run_in_threadpool(
        parse_upload,
        data,
        file.filename,
        fields=[str(item)[:128] for item in parsed_fields],
        max_records=service.settings.max_records,
        max_value_length=service.settings.max_field_value_length,
    )
    return {
        "document": format_document(
            [str(item)[:128] for item in parsed_fields],
            records=[dict(record) for record in report.document.records],
            schema=list(report.document.schema),
        ),
        "schema": list(report.document.schema),
        "record_count": len(report.document),
        "detected": report.detected,
        "warnings": report.warnings,
    }


@router.post("/records/validate", tags=["records"])
async def validate_records(payload: ValidateRecordsRequest, service: Service) -> dict[str, Any]:
    _check_document_size(payload.document, service)
    template, _, _ = service.resolve_template(payload.template_id, payload.layout_id)
    document = parse_document(
        payload.document,
        max_records=service.settings.max_records,
        max_value_length=service.settings.max_field_value_length,
    )
    return analyze(template.field_names, document)


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in bounded chunks, aborting past ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLarge(
                f"the uploaded file exceeds the {max_bytes} byte maximum",
                limit_name="max_upload_bytes",
                limit=max_bytes,
                actual=total,
            )
        chunks.append(chunk)
    if not chunks:
        raise ValidationError("the uploaded file is empty", loc=("file",))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


@router.post("/render/plan", tags=["render"])
async def render_plan(payload: RenderRequest, service: Service) -> dict[str, Any]:
    """Counts and diagnostics, no PDF. This is the UI's per-keystroke call."""
    _check_document_size(payload.document, service)
    plan = await run_in_threadpool(
        service.plan,
        template_id=payload.template_id,
        layout_id=payload.layout_id,
        document=payload.document,
        overrides=_overrides(payload),
    )
    return plan.to_dict()


@router.post("/render/preview", tags=["render"])
async def render_preview(payload: PreviewRequest, service: Service, request: Request) -> Response:
    """One page as a PNG. Returns bytes, not base64 wrapped in JSON."""
    _check_document_size(payload.document, service)

    etag = _preview_etag(payload)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    plan = await run_in_threadpool(
        service.plan,
        template_id=payload.template_id,
        layout_id=payload.layout_id,
        document=payload.document,
        overrides=_overrides(payload),
    )
    png = await run_in_threadpool(
        service.preview_png, plan, _options(payload), page=payload.page, scale=payload.scale
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=60",
            "X-Page-Count": str(plan.pages),
            "X-Label-Count": str(plan.labels),
        },
    )


@router.post("/render/pdf", tags=["render"])
async def render_pdf_endpoint(payload: RenderRequest, service: Service) -> Response:
    """The print-ready sheet."""
    _check_document_size(payload.document, service)
    plan = await run_in_threadpool(
        service.plan,
        template_id=payload.template_id,
        layout_id=payload.layout_id,
        document=payload.document,
        overrides=_overrides(payload),
    )
    result = await run_in_threadpool(service.render, plan, _options(payload))
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(payload.filename),
            "X-Label-Count": str(result.label_count),
            "X-Page-Count": str(result.page_count),
            # A per-element problem degrades to a warning rather than failing a
            # whole sheet, so the count has to travel or it is silent.
            "X-Render-Warnings": str(len(result.warnings)),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@router.get("/presets/avery", tags=["presets"])
async def list_avery_presets() -> dict[str, Any]:
    return {
        "presets": [
            {
                "code": preset.code,
                "aliases": list(preset.aliases),
                "description": preset.description,
                "rows": preset.rows,
                "cols": preset.cols,
                "stock": preset.stock,
                "source": preset.source,
            }
            for preset in iter_presets()
        ]
    }


@router.post("/presets/avery/{code}/template", tags=["presets"])
async def materialize_avery_preset(code: str) -> dict[str, Any]:
    """Return a preset as a template document. Never writes to disk."""
    if len(code) > MAX_PRESET_CODE_LENGTH or not code.replace("-", "").isalnum():
        raise NotFoundError(f"unknown Avery code {code!r}")
    return {"template": build_template(code).dump()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Characters allowed unescaped in the ASCII fallback filename. Anything else
#: -- quotes, semicolons, CR, LF, non-ASCII -- is dropped, because the old code
#: f-string-interpolated user input straight into the header value.
_FILENAME_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ()[]")


def sanitize_filename(raw: str | None) -> str:
    """Reduce a user-supplied name to a safe ``.pdf`` filename."""
    candidate = (raw or "labels").strip()
    cleaned = "".join(char for char in candidate if char in _FILENAME_SAFE)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(". ") or "labels"
    cleaned = cleaned[:100]
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned


def content_disposition(raw: str | None) -> str:
    """Build an RFC 6266 header with both an ASCII and a UTF-8 filename."""
    ascii_name = sanitize_filename(raw)
    original = (raw or "labels").strip()
    if not original.lower().endswith(".pdf"):
        original = f"{original}.pdf"
    encoded = quote(original[:100], safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _preview_etag(payload: PreviewRequest) -> str:
    digest = hashlib.sha256(payload.model_dump_json(exclude_none=False).encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'
