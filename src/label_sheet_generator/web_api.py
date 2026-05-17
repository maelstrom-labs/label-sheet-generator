from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from label_sheet_generator.models import TemplateError
from label_sheet_generator.workspace import (
    WorkspaceConfig,
    format_template_option,
    get_template_catalog,
    get_workspace_config,
    normalize_download_filename,
    prepare_render_state,
    render_workspace_pdf,
    render_workspace_preview,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"


class TemplateOptionModel(BaseModel):
    key: str
    template_name: str | None
    template_type: Literal["label", "text-layout"]
    display_name: str


class BootstrapResponse(BaseModel):
    label_templates: list[TemplateOptionModel]
    layout_templates: list[TemplateOptionModel]


class WorkspaceConfigRequest(BaseModel):
    label_template_key: str
    layout_template_key: str | None = None


class WorkspaceConfigResponse(BaseModel):
    label_template_name: str
    layout_name: str
    fields: list[str]
    labels_per_page: int
    preview_settings_error: str | None
    document: str
    margin_top_mm: float
    margin_right_mm: float
    margin_bottom_mm: float
    margin_left_mm: float


class RenderRequest(BaseModel):
    label_template_key: str
    layout_template_key: str | None = None
    data_document: str
    margin_top_mm: float
    margin_right_mm: float
    margin_bottom_mm: float
    margin_left_mm: float
    page_orientation: Literal["portrait", "landscape"] = "portrait"
    page_rotation_deg: Literal[0, 90, 180, 270] = 0
    text_rotation_deg: float | None = None
    outline_slots: bool = True
    download_filename: str | None = None


class PreviewResponse(BaseModel):
    label_template_name: str
    layout_name: str
    labels: int
    pages: int
    fields: list[str]
    missing_fields: list[str]
    extra_fields: list[str]
    preview_settings_error: str | None
    record_document_error: str | None
    render_runtime_error: str | None
    preview_image_base64: str | None


def _ui_not_built_response() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang=\"en\">
          <head>
            <meta charset=\"utf-8\" />
            <title>Label Sheet Generator</title>
            <style>
              body {
                margin: 0;
                padding: 48px;
                background: #0c1626;
                color: #f8fafc;
                font: 16px/1.6 system-ui, sans-serif;
              }
              code {
                color: #7ed3e6;
              }
            </style>
          </head>
          <body>
            <h1>Frontend build not found</h1>
            <p>Build the React app from <code>frontend/</code> with <code>npm install</code> and <code>npm run build</code>, or run <code>npm run dev</code> for local development.</p>
          </body>
        </html>
        """,
        status_code=503,
    )


def _template_option_model(template) -> TemplateOptionModel:
    return TemplateOptionModel(
        key=template.key,
        template_name=template.template_name,
        template_type=template.template_type,
        display_name=format_template_option(template),
    )


def _workspace_config_response(config: WorkspaceConfig) -> WorkspaceConfigResponse:
    return WorkspaceConfigResponse(
        label_template_name=config.label_template_name,
        layout_name=config.layout_name,
        fields=config.fields,
        labels_per_page=config.labels_per_page,
        preview_settings_error=config.preview_settings_error,
        document=config.document,
        margin_top_mm=config.margin_top_mm,
        margin_right_mm=config.margin_right_mm,
        margin_bottom_mm=config.margin_bottom_mm,
        margin_left_mm=config.margin_left_mm,
    )


def _raise_bad_request(exc: Exception) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


app = FastAPI(title="Label Sheet Generator API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="assets")


@app.get("/api/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bootstrap", response_model=BootstrapResponse)
def bootstrap() -> BootstrapResponse:
    try:
        label_templates, layout_templates = get_template_catalog()
    except TemplateError as exc:
        _raise_bad_request(exc)
    return BootstrapResponse(
        label_templates=[_template_option_model(template) for template in label_templates],
        layout_templates=[_template_option_model(template) for template in layout_templates],
    )


@app.post("/api/workspace/config", response_model=WorkspaceConfigResponse)
def workspace_config(request: WorkspaceConfigRequest) -> WorkspaceConfigResponse:
    try:
        config = get_workspace_config(request.label_template_key, request.layout_template_key)
    except TemplateError as exc:
        _raise_bad_request(exc)
    return _workspace_config_response(config)


@app.post("/api/workspace/import")
async def import_workspace_data(
    file: UploadFile = File(...),
    fields: str = Form("[]"),
) -> dict[str, str]:
    from label_sheet_generator.workspace import convert_import_data_to_json

    try:
        parsed_fields = json.loads(fields)
        if not isinstance(parsed_fields, list):
            raise TemplateError("fields must be a JSON array")
        document = convert_import_data_to_json(
            file.filename or "records.json",
            await file.read(),
            fields=[str(field) for field in parsed_fields],
        )
    except (json.JSONDecodeError, TemplateError, UnicodeDecodeError) as exc:
        _raise_bad_request(exc)
    return {"document": document}


@app.post("/api/render/preview", response_model=PreviewResponse)
def render_preview(request: RenderRequest) -> PreviewResponse:
    try:
        config = get_workspace_config(request.label_template_key, request.layout_template_key)
        state = prepare_render_state(
            config,
            data_document=request.data_document,
            margin_top_mm=request.margin_top_mm,
            margin_right_mm=request.margin_right_mm,
            margin_bottom_mm=request.margin_bottom_mm,
            margin_left_mm=request.margin_left_mm,
        )
    except TemplateError as exc:
        _raise_bad_request(exc)

    preview_image_base64: str | None = None
    render_runtime_error: str | None = None
    if state.renderable:
        try:
            preview_image = render_workspace_preview(
                state,
                outline_slots=request.outline_slots,
                page_orientation=request.page_orientation,
                page_rotation_deg=request.page_rotation_deg,
                text_rotation_deg=request.text_rotation_deg,
            )
            preview_image_base64 = base64.b64encode(preview_image).decode("ascii")
        except (RuntimeError, TemplateError) as exc:
            render_runtime_error = str(exc)

    return PreviewResponse(
        label_template_name=config.label_template_name,
        layout_name=config.layout_name,
        labels=len(state.full_records),
        pages=state.page_count,
        fields=config.fields,
        missing_fields=state.missing_fields,
        extra_fields=state.extra_fields,
        preview_settings_error=config.preview_settings_error,
        record_document_error=state.record_document_error,
        render_runtime_error=render_runtime_error,
        preview_image_base64=preview_image_base64,
    )


@app.post("/api/render/pdf")
def render_pdf_file(request: RenderRequest) -> Response:
    try:
        config = get_workspace_config(request.label_template_key, request.layout_template_key)
        state = prepare_render_state(
            config,
            data_document=request.data_document,
            margin_top_mm=request.margin_top_mm,
            margin_right_mm=request.margin_right_mm,
            margin_bottom_mm=request.margin_bottom_mm,
            margin_left_mm=request.margin_left_mm,
        )
        if config.preview_settings_error is not None:
            raise TemplateError(config.preview_settings_error)
        if state.record_document_error is not None:
            raise TemplateError(state.record_document_error)
        if config.fields and not state.full_records:
            raise TemplateError("add at least one record before exporting")
        pdf_bytes = render_workspace_pdf(
            state,
            outline_slots=request.outline_slots,
            page_orientation=request.page_orientation,
            page_rotation_deg=request.page_rotation_deg,
            text_rotation_deg=request.text_rotation_deg,
        )
    except (RuntimeError, TemplateError) as exc:
        _raise_bad_request(exc)

    file_name = normalize_download_filename(request.download_filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@app.get("/", include_in_schema=False)
def spa_index() -> Response:
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return _ui_not_built_response()


@app.get("/{full_path:path}", include_in_schema=False)
def spa_paths(full_path: str) -> Response:
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)

    asset_path = FRONTEND_DIST_DIR / full_path
    if asset_path.exists() and asset_path.is_file():
        return FileResponse(asset_path)
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return _ui_not_built_response()