from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tempfile
from typing import Any, Literal
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph

from label_sheet_generator.models import BarcodeElement, ImageElement, LabelTemplate, TextElement, TemplateError
from label_sheet_generator.units import mm_to_pt


PageOrientation = Literal["portrait", "landscape"]


@dataclass(slots=True)
class LabelSlot:
    row: int
    col: int
    x_pt: float
    y_pt: float
    width_pt: float
    height_pt: float


class _SafeRecord(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def _normalize_page_orientation(page_orientation: str) -> PageOrientation:
    normalized_value = page_orientation.strip().lower()
    if normalized_value not in {"portrait", "landscape"}:
        raise TemplateError("page_orientation must be 'portrait' or 'landscape'")
    return normalized_value


def _normalize_page_rotation(page_rotation_deg: float | int) -> int:
    try:
        numeric_rotation = float(page_rotation_deg)
    except (TypeError, ValueError) as exc:
        raise TemplateError("page_rotation_deg must be 0, 90, 180, or 270") from exc

    normalized_rotation = numeric_rotation % 360.0
    for allowed_rotation in (0, 90, 180, 270):
        if math.isclose(normalized_rotation, allowed_rotation, abs_tol=1e-6):
            return allowed_rotation
    raise TemplateError("page_rotation_deg must be 0, 90, 180, or 270")


def _apply_page_rotation(output_path: Path, page_rotation_deg: int) -> None:
    if page_rotation_deg == 0:
        return

    reader = PdfReader(str(output_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page.rotate(page_rotation_deg))

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        rotated_output_path = Path(handle.name)

    try:
        with rotated_output_path.open("wb") as destination:
            writer.write(destination)
        rotated_output_path.replace(output_path)
    finally:
        rotated_output_path.unlink(missing_ok=True)


def compute_slots(template: LabelTemplate) -> list[LabelSlot]:
    if template.page.width_mm <= 0 or template.page.height_mm <= 0:
        raise TemplateError("page dimensions must be positive")
    if template.grid.rows <= 0 or template.grid.cols <= 0:
        raise TemplateError("grid rows and cols must be positive")

    page_width_pt = mm_to_pt(template.page.width_mm)
    page_height_pt = mm_to_pt(template.page.height_mm)
    label_width_pt = mm_to_pt(template.grid.resolved_label_width_mm(template.page))
    label_height_pt = mm_to_pt(template.grid.resolved_label_height_mm(template.page))
    margin_left_pt = mm_to_pt(template.grid.margin_left_mm)
    margin_top_pt = mm_to_pt(template.grid.margin_top_mm)
    gap_x_pt = mm_to_pt(template.grid.gap_x_mm)
    gap_y_pt = mm_to_pt(template.grid.gap_y_mm)

    slots: list[LabelSlot] = []
    for row in range(template.grid.rows):
        top_offset_pt = margin_top_pt + row * (label_height_pt + gap_y_pt)
        y_pt = page_height_pt - top_offset_pt - label_height_pt

        if y_pt < 0:
            raise TemplateError("grid extends below the page bounds")

        for col in range(template.grid.cols):
            x_pt = margin_left_pt + col * (label_width_pt + gap_x_pt)
            if x_pt + label_width_pt > page_width_pt + 0.01:
                raise TemplateError("grid extends beyond the page width")
            slots.append(
                LabelSlot(
                    row=row,
                    col=col,
                    x_pt=x_pt,
                    y_pt=y_pt,
                    width_pt=label_width_pt,
                    height_pt=label_height_pt,
                )
            )

    return slots


def _resolve_value(raw_element: TextElement | BarcodeElement | ImageElement, record: dict[str, Any]) -> Any:
    if raw_element.template is not None:
        return raw_element.template.format_map(_SafeRecord(record))
    if raw_element.field is not None:
        return record.get(raw_element.field)
    return raw_element.value


def _parse_color(color: str) -> Color:
    try:
        return HexColor(color)
    except ValueError as exc:
        raise TemplateError(f"unsupported color value: {color!r}") from exc


def _resolve_box(slot: LabelSlot, x_mm: float, y_mm: float, width_mm: float | None, height_mm: float | None) -> tuple[float, float, float, float]:
    width_pt = slot.width_pt - mm_to_pt(x_mm) if width_mm is None else mm_to_pt(width_mm)
    height_pt = slot.height_pt - mm_to_pt(y_mm) if height_mm is None else mm_to_pt(height_mm)
    if width_pt <= 0 or height_pt <= 0:
        raise TemplateError("element geometry produced a non-positive drawing box")

    x_pt = slot.x_pt + mm_to_pt(x_mm)
    top_pt = slot.y_pt + slot.height_pt - mm_to_pt(y_mm)
    y_pt = top_pt - height_pt
    return x_pt, y_pt, width_pt, height_pt


def _resolve_path(raw_path: str, candidate_directories: list[Path]) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path

    for directory in candidate_directories:
        candidate = directory / path
        if candidate.exists():
            return candidate
    return path


def _render_text(
    canvas: pdf_canvas.Canvas,
    slot: LabelSlot,
    element: TextElement,
    record: dict[str, Any],
    *,
    text_rotation_deg: float | None = None,
) -> None:
    value = _resolve_value(element, record)
    if value is None:
        return

    text = str(value)
    x_pt, y_pt, width_pt, height_pt = _resolve_box(slot, element.x_mm, element.y_mm, element.width_mm, element.height_mm)
    effective_rotation_deg = element.rotation_deg if text_rotation_deg is None else float(text_rotation_deg)
    normalized_rotation_deg = effective_rotation_deg % 360.0
    if math.isclose(normalized_rotation_deg % 180.0, 90.0, abs_tol=1e-6):
        layout_width_pt = height_pt
        layout_height_pt = width_pt
    else:
        layout_width_pt = width_pt
        layout_height_pt = height_pt
    alignment = {
        "left": TA_LEFT,
        "center": TA_CENTER,
        "right": TA_RIGHT,
        "justify": TA_JUSTIFY,
    }[element.align]
    style = ParagraphStyle(
        name="label-text",
        fontName=element.font_name,
        fontSize=element.font_size_pt,
        leading=element.leading_pt or element.font_size_pt * 1.2,
        textColor=_parse_color(element.color),
        alignment=alignment,
    )
    paragraph = Paragraph(escape(text).replace("\n", "<br/>"), style)
    _, rendered_height = paragraph.wrap(layout_width_pt, layout_height_pt)
    draw_x_pt = -(layout_width_pt / 2)
    draw_y_pt = -(layout_height_pt / 2) + max(layout_height_pt - rendered_height, 0)

    canvas.saveState()
    canvas.translate(x_pt + width_pt / 2, y_pt + height_pt / 2)
    if not math.isclose(normalized_rotation_deg, 0.0, abs_tol=1e-6):
        canvas.rotate(normalized_rotation_deg)
    paragraph.drawOn(canvas, draw_x_pt, draw_y_pt)
    canvas.restoreState()


def _barcode_kind(barcode_type: str) -> str:
    mapping = {
        "code128": "Code128",
        "ean13": "EAN13",
        "qr": "QR",
    }
    try:
        return mapping[barcode_type]
    except KeyError as exc:
        raise TemplateError(f"unsupported barcode type: {barcode_type}") from exc


def _render_barcode(canvas: pdf_canvas.Canvas, slot: LabelSlot, element: BarcodeElement, record: dict[str, Any]) -> None:
    value = _resolve_value(element, record)
    if value in {None, ""}:
        return

    barcode_value = str(value)
    if element.barcode_type == "ean13":
        digits = "".join(character for character in barcode_value if character.isdigit())
        if len(digits) == 13:
            digits = digits[:12]
        if len(digits) != 12:
            raise TemplateError("EAN13 barcodes require 12 digits or 13 digits including the check digit")
        barcode_value = digits

    x_pt, y_pt, width_pt, height_pt = _resolve_box(slot, element.x_mm, element.y_mm, element.width_mm, element.height_mm)
    drawing = createBarcodeDrawing(
        _barcode_kind(element.barcode_type),
        value=barcode_value,
        humanReadable=element.human_readable,
    )

    if drawing.width <= 0 or drawing.height <= 0:
        raise TemplateError("barcode drawing produced invalid dimensions")

    scale = min(width_pt / drawing.width, height_pt / drawing.height)
    canvas.saveState()
    canvas.translate(x_pt, y_pt)
    canvas.scale(scale, scale)
    renderPDF.draw(drawing, canvas, 0, 0)
    canvas.restoreState()


def _render_image(
    canvas: pdf_canvas.Canvas,
    slot: LabelSlot,
    element: ImageElement,
    record: dict[str, Any],
    candidate_directories: list[Path],
) -> None:
    value = _resolve_value(element, record)
    if value in {None, ""}:
        return

    image_path = _resolve_path(str(value), candidate_directories)
    image = ImageReader(str(image_path))
    image_width_px, image_height_px = image.getSize()
    x_pt, y_pt, width_pt, height_pt = _resolve_box(slot, element.x_mm, element.y_mm, element.width_mm, element.height_mm)

    if element.preserve_aspect:
        scale = min(width_pt / image_width_px, height_pt / image_height_px)
        draw_width_pt = image_width_px * scale
        draw_height_pt = image_height_px * scale
    else:
        draw_width_pt = width_pt
        draw_height_pt = height_pt

    draw_y_pt = y_pt + (height_pt - draw_height_pt)
    canvas.drawImage(
        image,
        x_pt,
        draw_y_pt,
        width=draw_width_pt,
        height=draw_height_pt,
        preserveAspectRatio=False,
        mask="auto",
    )


def _draw_bleed_guides(
    canvas: pdf_canvas.Canvas,
    slot: LabelSlot,
    *,
    bleed_guide_inset_mm: float,
) -> None:
    inset_pt = mm_to_pt(bleed_guide_inset_mm)
    inner_width_pt = slot.width_pt - inset_pt * 2
    inner_height_pt = slot.height_pt - inset_pt * 2
    if inner_width_pt <= 0 or inner_height_pt <= 0:
        raise TemplateError("bleed_guide_inset_mm is too large for the label dimensions")

    canvas.saveState()
    canvas.setStrokeColorRGB(0.14, 0.82, 0.86)
    canvas.setLineWidth(0.6)
    canvas.setDash(4, 2)
    canvas.rect(
        slot.x_pt + inset_pt,
        slot.y_pt + inset_pt,
        inner_width_pt,
        inner_height_pt,
        stroke=1,
        fill=0,
    )
    canvas.restoreState()


def render_pdf(
    template: LabelTemplate,
    records: list[dict[str, Any]],
    output_path: str | Path,
    template_path: str | Path,
    records_path: str | Path | None = None,
    outline_slots: bool = False,
    bleed_guide_inset_mm: float | None = None,
    page_orientation: str = "portrait",
    page_rotation_deg: int = 0,
    text_rotation_deg: float | None = None,
) -> None:
    candidate_directories = [Path(template_path).resolve().parent]
    if records_path is not None:
        candidate_directories.insert(0, Path(records_path).resolve().parent)

    normalized_orientation = _normalize_page_orientation(page_orientation)
    normalized_page_rotation_deg = _normalize_page_rotation(page_rotation_deg)
    normalized_records = records or [{}]
    slots = compute_slots(template)
    page_width_pt = mm_to_pt(template.page.width_mm)
    page_height_pt = mm_to_pt(template.page.height_mm)
    labels_per_page = len(slots)

    if normalized_orientation == "landscape":
        output_page_width_pt = page_height_pt
        output_page_height_pt = page_width_pt
    else:
        output_page_width_pt = page_width_pt
        output_page_height_pt = page_height_pt

    canvas = pdf_canvas.Canvas(str(output_path), pagesize=(output_page_width_pt, output_page_height_pt))

    for offset in range(0, len(normalized_records), labels_per_page):
        page_records = normalized_records[offset : offset + labels_per_page]

        if normalized_orientation == "landscape":
            canvas.saveState()
            canvas.translate(output_page_width_pt, 0)
            canvas.rotate(90)

        for slot, record in zip(slots, page_records):
            canvas.saveState()
            clip_path = canvas.beginPath()
            clip_path.rect(slot.x_pt, slot.y_pt, slot.width_pt, slot.height_pt)
            canvas.clipPath(clip_path, stroke=0, fill=0)

            if outline_slots:
                canvas.setStrokeColorRGB(0.8, 0.8, 0.8)
                canvas.rect(slot.x_pt, slot.y_pt, slot.width_pt, slot.height_pt, stroke=1, fill=0)
            if bleed_guide_inset_mm is not None:
                _draw_bleed_guides(canvas, slot, bleed_guide_inset_mm=bleed_guide_inset_mm)

            for element in template.elements:
                if isinstance(element, TextElement):
                    _render_text(canvas, slot, element, record, text_rotation_deg=text_rotation_deg)
                elif isinstance(element, BarcodeElement):
                    _render_barcode(canvas, slot, element, record)
                else:
                    _render_image(canvas, slot, element, record, candidate_directories)

            canvas.restoreState()

        if normalized_orientation == "landscape":
            canvas.restoreState()

        canvas.showPage()

    canvas.save()
    _apply_page_rotation(Path(output_path), normalized_page_rotation_deg)
