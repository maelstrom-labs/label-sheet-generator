from __future__ import annotations

import csv
import io
import json
import math
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from string import Formatter
from typing import Any, Iterable, Sequence

from label_sheet_generator.io import load_template, load_text_layout_template
from label_sheet_generator.models import Element, LabelTemplate, TemplateError, TextLayoutTemplate
from label_sheet_generator.presets import LocalTemplate, iter_local_templates, resolve_template_input_path
from label_sheet_generator.rendering import compute_slots, render_pdf


DEFAULT_BLEED_GUIDE_INSET_MM = 1.5


@dataclass(slots=True)
class WorkspaceConfig:
    label_template: LocalTemplate
    layout_template: LocalTemplate | None
    label_template_name: str
    layout_name: str
    effective_template: LabelTemplate
    render_template_path: Path
    fields: list[str]
    labels_per_page: int
    preview_settings_error: str | None
    document: str
    margin_top_mm: float
    margin_right_mm: float
    margin_bottom_mm: float
    margin_left_mm: float


@dataclass(slots=True)
class WorkspaceRenderState:
    config: WorkspaceConfig
    working_template: LabelTemplate
    declared_schema: list[str]
    records: list[dict[str, Any]]
    record_document_error: str | None
    missing_fields: list[str]
    extra_fields: list[str]
    full_records: list[dict[str, Any]]
    preview_records: list[dict[str, Any]]
    page_count: int

    @property
    def renderable(self) -> bool:
        if self.config.preview_settings_error is not None or self.record_document_error is not None:
            return False
        if self.config.fields and not self.full_records:
            return False
        return True


def _normalize_template_field_name(field_name: str) -> str:
    return field_name.split(".", 1)[0].split("[", 1)[0]


def extract_field_names(elements: Iterable[Element]) -> list[str]:
    discovered_fields: list[str] = []
    seen_fields: set[str] = set()

    def remember(field_name: str | None) -> None:
        if not field_name:
            return
        normalized_name = _normalize_template_field_name(field_name)
        if not normalized_name or normalized_name in seen_fields:
            return
        seen_fields.add(normalized_name)
        discovered_fields.append(normalized_name)

    for element in elements:
        remember(element.field)
        if element.template is not None:
            for _, field_name, _, _ in Formatter().parse(element.template):
                remember(field_name)

    if "name" in seen_fields:
        return ["name", *(field for field in discovered_fields if field != "name")]
    return discovered_fields


def build_records_from_field_inputs(field_inputs: dict[str, str]) -> list[dict[str, str]]:
    normalized_columns: dict[str, list[str]] = {}
    for field_name, raw_text in field_inputs.items():
        if raw_text.strip() == "":
            continue
        normalized_columns[field_name] = [line.strip() for line in raw_text.splitlines()]

    record_count = max((len(values) for values in normalized_columns.values()), default=0)
    records: list[dict[str, str]] = []
    for index in range(record_count):
        records.append(
            {
                field_name: values[index] if index < len(values) else ""
                for field_name, values in normalized_columns.items()
            }
        )
    return records


def _normalize_record_schema_name(field_name: Any) -> str:
    if not isinstance(field_name, str):
        field_name = str(field_name)
    return _normalize_template_field_name(field_name.strip())


def _normalize_record_list(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_records: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise TemplateError(f"record {index} must be a mapping")
        normalized_records.append(dict(record))
    return normalized_records


def build_record_schema(
    fields: Sequence[str],
    *,
    declared_schema: Sequence[Any] | None = None,
    records: Sequence[dict[str, Any]] | None = None,
) -> list[str]:
    schema: list[str] = []
    seen_fields: set[str] = set()

    def remember(field_name: Any) -> None:
        normalized_name = _normalize_record_schema_name(field_name)
        if not normalized_name or normalized_name in seen_fields:
            return
        seen_fields.add(normalized_name)
        schema.append(normalized_name)

    for field_name in fields:
        remember(field_name)
    for field_name in declared_schema or []:
        remember(field_name)
    for record in records or []:
        for field_name in record:
            remember(field_name)

    return schema


def build_record_document(
    fields: Sequence[str],
    *,
    records: Sequence[dict[str, Any]] | None = None,
    declared_schema: Sequence[Any] | None = None,
) -> dict[str, Any]:
    normalized_records = _normalize_record_list(list(records or []))
    return {
        "schema": build_record_schema(fields, declared_schema=declared_schema, records=normalized_records),
        "records": normalized_records,
    }


def format_record_document(
    fields: Sequence[str],
    *,
    records: Sequence[dict[str, Any]] | None = None,
    declared_schema: Sequence[Any] | None = None,
) -> str:
    return json.dumps(
        build_record_document(fields, records=records, declared_schema=declared_schema),
        indent=2,
    )


def parse_record_document(raw_text: str) -> tuple[list[str], list[dict[str, Any]]]:
    stripped_text = raw_text.strip()
    if stripped_text == "":
        return [], []

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"label data must be valid JSON: {exc.msg}") from exc

    if isinstance(raw_data, list):
        return [], _normalize_record_list(raw_data)

    if not isinstance(raw_data, dict):
        raise TemplateError("label data must be a JSON array or an object with a 'records' array")

    raw_schema = raw_data.get("schema", [])
    if raw_schema is None:
        raw_schema = []
    if not isinstance(raw_schema, list):
        raise TemplateError("label data 'schema' must be an array when provided")

    if "records" not in raw_data:
        raise TemplateError("label data must be a JSON array or an object with a 'records' array")
    raw_records = raw_data["records"]
    if not isinstance(raw_records, list):
        raise TemplateError("label data 'records' must be an array")

    return [str(item) for item in raw_schema], _normalize_record_list(raw_records)


def parse_csv_records(raw_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw_text))
    if reader.fieldnames is None:
        raise TemplateError("CSV import requires a header row")

    records: list[dict[str, str]] = []
    for row in reader:
        normalized_row = {
            str(field_name).strip(): "" if value is None else value.strip()
            for field_name, value in row.items()
            if field_name is not None and str(field_name).strip() != ""
        }
        if normalized_row and any(value != "" for value in normalized_row.values()):
            records.append(normalized_row)

    return records


def convert_import_data_to_json(
    file_name: str,
    file_bytes: bytes,
    *,
    fields: Sequence[str],
) -> str:
    suffix = Path(file_name).suffix.lower()
    raw_text = file_bytes.decode("utf-8-sig")

    if suffix == ".csv":
        records = parse_csv_records(raw_text)
        return format_record_document(fields, records=records)

    if suffix == ".json":
        declared_schema, records = parse_record_document(raw_text)
        return format_record_document(fields, records=records, declared_schema=declared_schema)

    raise TemplateError(f"unsupported import format: {suffix or '<none>'}")


def apply_layout_template(
    label_template: LabelTemplate,
    layout_template: TextLayoutTemplate,
    *,
    layout_path: Path,
) -> LabelTemplate:
    metadata = dict(label_template.metadata)
    metadata["layout_template"] = layout_template.name or layout_path.stem
    return LabelTemplate(
        page=label_template.page,
        grid=label_template.grid,
        elements=list(layout_template.elements),
        name=label_template.name,
        metadata=metadata,
    )


def apply_margin_overrides(
    template: LabelTemplate,
    *,
    margin_top_mm: float,
    margin_right_mm: float,
    margin_bottom_mm: float,
    margin_left_mm: float,
) -> LabelTemplate:
    metadata = dict(template.metadata)
    metadata["ui_margin_overrides_mm"] = {
        "top": float(margin_top_mm),
        "right": float(margin_right_mm),
        "bottom": float(margin_bottom_mm),
        "left": float(margin_left_mm),
    }
    return LabelTemplate(
        page=template.page,
        grid=replace(
            template.grid,
            margin_top_mm=float(margin_top_mm),
            margin_right_mm=float(margin_right_mm),
            margin_bottom_mm=float(margin_bottom_mm),
            margin_left_mm=float(margin_left_mm),
        ),
        elements=list(template.elements),
        name=template.name,
        metadata=metadata,
    )


def analyze_record_alignment(
    fields: Sequence[str],
    *,
    declared_schema: Sequence[Any] | None = None,
    records: Sequence[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    available_schema = build_record_schema([], declared_schema=declared_schema, records=records)
    missing_fields = [field for field in fields if field not in available_schema]
    extra_fields = [field for field in available_schema if field not in fields]
    return available_schema, missing_fields, extra_fields


def render_pdf_bytes(
    template: LabelTemplate,
    records: Sequence[dict[str, Any]],
    *,
    template_path: Path,
    outline_slots: bool = False,
    bleed_guide_inset_mm: float | None = None,
    page_orientation: str = "portrait",
    page_rotation_deg: int = 0,
    text_rotation_deg: float | None = None,
) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        output_path = Path(handle.name)

    try:
        render_pdf(
            template,
            list(records),
            output_path,
            template_path=template_path,
            outline_slots=outline_slots,
            bleed_guide_inset_mm=bleed_guide_inset_mm,
            page_orientation=page_orientation,
            page_rotation_deg=page_rotation_deg,
            text_rotation_deg=text_rotation_deg,
        )
        return output_path.read_bytes()
    finally:
        output_path.unlink(missing_ok=True)


def render_preview_image_bytes(pdf_bytes: bytes, *, scale: float = 2.0) -> bytes:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF preview rendering requires pypdfium2. Reinstall the UI extras with `python -m pip install -e '.[ui]'`."
        ) from exc

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        source_path = Path(handle.name)
        handle.write(pdf_bytes)

    try:
        document = pdfium.PdfDocument(str(source_path))
        page = document[0]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        page.close()
        document.close()
        return buffer.getvalue()
    finally:
        source_path.unlink(missing_ok=True)


def format_template_option(template: LocalTemplate) -> str:
    display_name = template.template_name or template.key
    return f"{display_name} [{template.key}]"


def split_templates_by_type(templates: Iterable[LocalTemplate]) -> tuple[list[LocalTemplate], list[LocalTemplate]]:
    label_templates: list[LocalTemplate] = []
    layout_templates: list[LocalTemplate] = []
    for template in templates:
        if template.template_type == "label":
            label_templates.append(template)
        else:
            layout_templates.append(template)
    return label_templates, layout_templates


def get_template_catalog(base_dir: str | Path = ".") -> tuple[list[LocalTemplate], list[LocalTemplate]]:
    templates = iter_local_templates(base_dir)
    label_templates, layout_templates = split_templates_by_type(templates)
    if not label_templates:
        raise TemplateError("No label templates were found. Add a template under templates/labels/ or templates/.")
    return label_templates, layout_templates


def get_workspace_config(
    label_template_key: str,
    layout_template_key: str | None = None,
    *,
    base_dir: str | Path = ".",
) -> WorkspaceConfig:
    label_templates, layout_templates = get_template_catalog(base_dir)
    label_template_lookup = {template.key: template for template in label_templates}
    layout_template_lookup = {template.key: template for template in layout_templates}

    try:
        selected_label_template = label_template_lookup[label_template_key]
    except KeyError as exc:
        raise TemplateError(f"unknown label template: {label_template_key}") from exc

    if layout_template_key is not None and layout_template_key not in layout_template_lookup:
        raise TemplateError(f"unknown text layout template: {layout_template_key}")

    label_template_path = resolve_template_input_path(selected_label_template.key, base_dir=base_dir)
    label_template = load_template(label_template_path)
    effective_template = label_template
    render_template_path = label_template_path
    selected_layout_template = None

    if layout_template_key is not None:
        selected_layout_template = layout_template_lookup[layout_template_key]
        layout_template_path = resolve_template_input_path(selected_layout_template.key, base_dir=base_dir)
        layout_template = load_text_layout_template(layout_template_path)
        effective_template = apply_layout_template(label_template, layout_template, layout_path=layout_template_path)
        render_template_path = layout_template_path

    label_template_name = selected_label_template.template_name or selected_label_template.key
    layout_name = (
        "Default Layout"
        if selected_layout_template is None
        else (selected_layout_template.template_name or selected_layout_template.key)
    )
    fields = extract_field_names(effective_template.elements)
    labels_per_page = 0
    preview_settings_error: str | None = None
    try:
        labels_per_page = len(compute_slots(effective_template))
    except TemplateError as exc:
        preview_settings_error = str(exc)

    return WorkspaceConfig(
        label_template=selected_label_template,
        layout_template=selected_layout_template,
        label_template_name=label_template_name,
        layout_name=layout_name,
        effective_template=effective_template,
        render_template_path=render_template_path,
        fields=fields,
        labels_per_page=labels_per_page,
        preview_settings_error=preview_settings_error,
        document=format_record_document(fields),
        margin_top_mm=float(label_template.grid.margin_top_mm),
        margin_right_mm=float(label_template.grid.margin_right_mm),
        margin_bottom_mm=float(label_template.grid.margin_bottom_mm),
        margin_left_mm=float(label_template.grid.margin_left_mm),
    )


def prepare_render_state(
    config: WorkspaceConfig,
    *,
    data_document: str,
    margin_top_mm: float,
    margin_right_mm: float,
    margin_bottom_mm: float,
    margin_left_mm: float,
) -> WorkspaceRenderState:
    working_template = apply_margin_overrides(
        config.effective_template,
        margin_top_mm=margin_top_mm,
        margin_right_mm=margin_right_mm,
        margin_bottom_mm=margin_bottom_mm,
        margin_left_mm=margin_left_mm,
    )

    declared_schema: list[str] = []
    records: list[dict[str, Any]] = []
    record_document_error: str | None = None
    try:
        declared_schema, records = parse_record_document(data_document)
    except TemplateError as exc:
        record_document_error = str(exc)

    _, missing_fields, extra_fields = analyze_record_alignment(
        config.fields,
        declared_schema=declared_schema,
        records=records,
    )

    if record_document_error is None:
        full_records = records or ([{}] if not config.fields else [])
    else:
        full_records = []

    preview_records = full_records[: config.labels_per_page] if full_records and config.labels_per_page else []
    page_count = math.ceil(len(full_records) / config.labels_per_page) if full_records and config.labels_per_page else 0

    return WorkspaceRenderState(
        config=config,
        working_template=working_template,
        declared_schema=declared_schema,
        records=records,
        record_document_error=record_document_error,
        missing_fields=missing_fields,
        extra_fields=extra_fields,
        full_records=full_records,
        preview_records=preview_records,
        page_count=page_count,
    )


def render_workspace_preview(
    state: WorkspaceRenderState,
    *,
    outline_slots: bool,
    page_orientation: str,
    page_rotation_deg: int,
    text_rotation_deg: float | None,
    preview_scale: float = 2.0,
) -> bytes:
    preview_pdf = render_pdf_bytes(
        state.working_template,
        state.preview_records,
        template_path=state.config.render_template_path,
        outline_slots=outline_slots,
        bleed_guide_inset_mm=None,
        page_orientation=page_orientation,
        page_rotation_deg=page_rotation_deg,
        text_rotation_deg=text_rotation_deg,
    )
    return render_preview_image_bytes(preview_pdf, scale=preview_scale)


def render_workspace_pdf(
    state: WorkspaceRenderState,
    *,
    outline_slots: bool,
    page_orientation: str,
    page_rotation_deg: int,
    text_rotation_deg: float | None,
) -> bytes:
    return render_pdf_bytes(
        state.working_template,
        state.full_records,
        template_path=state.config.render_template_path,
        outline_slots=outline_slots,
        bleed_guide_inset_mm=None,
        page_orientation=page_orientation,
        page_rotation_deg=page_rotation_deg,
        text_rotation_deg=text_rotation_deg,
    )


def normalize_download_filename(file_name: str | None) -> str:
    cleaned_name = (file_name or "labels").strip() or "labels"
    if not cleaned_name.lower().endswith(".pdf"):
        return f"{cleaned_name}.pdf"
    return cleaned_name