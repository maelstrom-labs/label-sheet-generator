"""The renderer: a validated template plus records in, PDF bytes out.

Three defects in the previous implementation shaped this module.

*Output went to a path.* ``render_pdf`` took an ``output_path``, wrote to it,
reopened it to apply page rotation, and rewrote it. A failure halfway through
left the previous PDF in place, so a request that errored still served stale
content, and every caller that wanted bytes paid a write and a read. Here the
canvas writes to a :class:`io.BytesIO` and nothing touches the filesystem.

*Rotation was a post-process.* The old code saved the file, reopened it with
``PdfReader``/``PdfWriter`` and replaced it. ``/Rotate`` is a page attribute
ReportLab can set directly, so pypdf is not a dependency of rendering.

*A failing element lost its identity.* One bad record raised out of the whole
render with no indication of which record or which element caused it. Every
element is drawn inside a handler that attaches ``record_index`` and
``element_index``; in strict mode that error is raised, otherwise it becomes a
:class:`RenderWarning` and the rest of the sheet still prints.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from reportlab.pdfgen import canvas as pdf_canvas

from label_sheet_generator.assets import AssetLoader
from label_sheet_generator.errors import (
    GeometryError,
    LabelSheetError,
    LimitExceeded,
    RecordError,
    RenderError,
    ValidationError,
)
from label_sheet_generator.geometry import Slot, compute_slots
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.render.barcode import draw_barcode
from label_sheet_generator.render.image import draw_image
from label_sheet_generator.render.text import draw_text
from label_sheet_generator.schema import (
    BarcodeElement,
    ImageElement,
    LabelTemplate,
    TextElement,
)
from label_sheet_generator.units import mm_to_pt

#: Grey level of the slot outline. Light enough to be ignored by the eye when
#: checking alignment, dark enough to survive a draft-quality print.
SLOT_OUTLINE_GREY = 0.8

#: Hairline weight for the slot outline, in points.
SLOT_OUTLINE_WIDTH_PT = 0.5

#: Cyan dashed bleed guide, carried over from the previous implementation so
#: existing proofs look the same. Cyan because it is the one ink that reads as
#: "not part of the artwork" on a colour proof.
BLEED_GUIDE_RGB = (0.14, 0.82, 0.86)
BLEED_GUIDE_WIDTH_PT = 0.6
BLEED_GUIDE_DASH_PT = (4, 2)

#: Landscape is produced by rotating the whole sheet a quarter turn.
LANDSCAPE_ROTATION_DEG = 90

_ORIENTATIONS = ("portrait", "landscape")
_PAGE_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Presentation choices that are not part of the template itself."""

    page_orientation: Literal["portrait", "landscape"] = "portrait"
    page_rotation_deg: Literal[0, 90, 180, 270] = 0
    text_rotation_deg: float | None = None
    outline_slots: bool = False
    bleed_guide_inset_mm: float | None = None
    #: True: a per-record problem aborts the render. False: it is recorded as
    #: a warning and the remaining labels are still drawn.
    strict: bool = False

    def validate(self) -> None:
        """Check the fields a type annotation cannot enforce at runtime.

        ``Literal`` is a static claim; these values usually arrive from a
        request body, so they are checked here rather than trusted.

        Raises:
            ValidationError: if an option is outside its allowed set.
        """
        if self.page_orientation not in _ORIENTATIONS:
            raise ValidationError(
                f"page_orientation must be one of {', '.join(_ORIENTATIONS)}",
                loc=("page_orientation",),
            )
        if self.page_rotation_deg not in _PAGE_ROTATIONS:
            raise ValidationError(
                "page_rotation_deg must be 0, 90, 180 or 270",
                loc=("page_rotation_deg",),
            )


#: Shared default so the signature of :func:`render_pdf` does not construct a
#: new instance on every call; RenderOptions is frozen, so sharing is safe.
DEFAULT_OPTIONS = RenderOptions()


@dataclass(frozen=True, slots=True)
class RenderWarning:
    """A problem that did not stop the render, attributed to its source."""

    code: str
    message: str
    record_index: int | None = None
    element_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, omitting attribution that is not known."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.record_index is not None:
            payload["record_index"] = self.record_index
        if self.element_index is not None:
            payload["element_index"] = self.element_index
        return payload


@dataclass(frozen=True, slots=True)
class RenderResult:
    """The finished document and what happened while producing it."""

    pdf_bytes: bytes
    page_count: int
    label_count: int
    warnings: list[RenderWarning] = field(default_factory=list)


def render_pdf(
    template: LabelTemplate,
    records: Sequence[dict[str, Any]],
    *,
    options: RenderOptions = DEFAULT_OPTIONS,
    assets: AssetLoader | None = None,
    max_pages: int | None = None,
    max_output_bytes: int | None = None,
) -> RenderResult:
    """Render ``records`` onto ``template``'s grid and return the PDF bytes.

    Records fill the grid in reading order, one page per ``rows * cols``
    records. An empty record list renders exactly one page in which every slot
    draws the template's static content, which is what a caller previewing a
    sheet wants and what the old ``records or [{}]`` approximated.

    Args:
        template: A validated template. Its geometry is checked here.
        records: Flat field maps, one per label.
        options: Presentation options; see :class:`RenderOptions`.
        assets: Image loader. ``None`` disables image elements.
        max_pages: Refuse to render more pages than this.
        max_output_bytes: Refuse to return a document larger than this.

    Returns:
        A :class:`RenderResult` whose ``warnings`` list is empty unless
        non-strict mode absorbed a per-element failure.

    Raises:
        RecordError: if ``records`` is not a sequence of field maps.
        ValidationError: if an option is invalid.
        GeometryError: if the grid or a bleed guide does not fit.
        LimitExceeded: if ``max_pages`` or ``max_output_bytes`` is exceeded.
        RenderError: in strict mode, for any element that cannot be drawn.
    """
    options.validate()
    normalized_records = _normalize_records(records)

    report = validate_geometry(template)
    # compute_slots re-runs the same pure arithmetic and raises on the fatal
    # issues; the report is kept for the warnings, which are advisory.
    slots = compute_slots(template)
    warnings = [RenderWarning(code=issue.code, message=issue.message) for issue in report.warnings]

    cells_per_page = template.grid.cells_per_page
    if not normalized_records:
        normalized_records = [{} for _ in range(cells_per_page)]

    page_count = -(-len(normalized_records) // cells_per_page)  # ceiling division
    if max_pages is not None and page_count > max_pages:
        # Checked before any drawing: the point of the limit is to refuse the
        # work, not to do it and then throw the result away.
        raise LimitExceeded(
            f"{len(normalized_records)} record(s) at {cells_per_page} per page need "
            f"{page_count} pages, which is over the limit of {max_pages}",
            limit_name="max_pages",
            limit=max_pages,
            actual=page_count,
        )

    inset_pt = _bleed_inset_pt(options.bleed_guide_inset_mm, slots[0])

    page_width_pt = mm_to_pt(template.page.width_mm)
    page_height_pt = mm_to_pt(template.page.height_mm)
    landscape = options.page_orientation == "landscape"
    output_width_pt = page_height_pt if landscape else page_width_pt
    output_height_pt = page_width_pt if landscape else page_height_pt

    buffer = io.BytesIO()
    canvas = pdf_canvas.Canvas(buffer, pagesize=(output_width_pt, output_height_pt))

    for page_index in range(page_count):
        start = page_index * cells_per_page
        page_records = normalized_records[start : start + cells_per_page]

        # /Rotate is a viewer instruction: it turns the page for display and
        # printing without moving any content, which is what a caller wanting
        # a sideways sheet from a portrait layout usually means.
        canvas.setPageRotation(options.page_rotation_deg)

        if landscape:
            # Swapping the mediabox and rotating the coordinate system maps a
            # portrait point (x, y) to (H - y, x) exactly, so every slot and
            # element below stays in portrait coordinates. The whole sheet is
            # turned, so the content reads sideways relative to the new page.
            canvas.saveState()
            canvas.translate(output_width_pt, 0)
            canvas.rotate(LANDSCAPE_ROTATION_DEG)

        for offset, record in enumerate(page_records):
            _draw_label(
                canvas,
                slots[offset],
                record,
                record_index=start + offset,
                template=template,
                options=options,
                assets=assets,
                label_width_mm=report.label_width_mm,
                label_height_mm=report.label_height_mm,
                inset_pt=inset_pt,
                warnings=warnings,
            )

        if landscape:
            canvas.restoreState()
        canvas.showPage()

    try:
        canvas.save()
    except LabelSheetError:
        raise
    except Exception as exc:
        raise RenderError(f"the PDF could not be serialised ({type(exc).__name__})") from exc

    pdf_bytes = buffer.getvalue()
    if max_output_bytes is not None and len(pdf_bytes) > max_output_bytes:
        raise LimitExceeded(
            f"the rendered PDF is {len(pdf_bytes)} bytes, over the limit of "
            f"{max_output_bytes}; render fewer records or simplify the template",
            limit_name="max_output_bytes",
            limit=max_output_bytes,
            actual=len(pdf_bytes),
        )

    return RenderResult(
        pdf_bytes=pdf_bytes,
        page_count=page_count,
        label_count=len(normalized_records),
        warnings=warnings,
    )


def _normalize_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy records into a list of plain dicts, rejecting anything else."""
    # Typed as a sequence, but the usual caller is a decoded request body, so
    # the shape is checked rather than trusted. A string is a sequence of
    # characters, which would otherwise be accepted and then fail per item.
    candidate: Any = records
    if isinstance(candidate, (str, bytes)) or not isinstance(candidate, Sequence):
        raise RecordError("records must be a list of field maps", loc=("records",))

    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise RecordError(
                f"record {index + 1} is a {type(record).__name__}, not a field map",
                loc=("records", index),
            )
        # Keys reach ``record.get`` and template substitution, both of which
        # assume strings; a non-string key would simply never match, which is
        # harder to diagnose than a rejection.
        for key in record:
            if not isinstance(key, str):
                raise RecordError(
                    f"record {index + 1} has a non-string field name",
                    loc=("records", index),
                )
        normalized.append(dict(record))
    return normalized


def _bleed_inset_pt(bleed_guide_inset_mm: float | None, slot: Slot) -> float | None:
    """Validate the bleed inset once, against the label size.

    Raises:
        GeometryError: if the inset is not positive or leaves no inner area.
    """
    if bleed_guide_inset_mm is None:
        return None

    inset_pt = mm_to_pt(bleed_guide_inset_mm)
    if inset_pt <= 0:
        raise GeometryError(
            "bleed_guide_inset_mm must be greater than zero",
            loc=("bleed_guide_inset_mm",),
        )
    if slot.width_pt - 2 * inset_pt <= 0 or slot.height_pt - 2 * inset_pt <= 0:
        raise GeometryError(
            f"a bleed inset of {bleed_guide_inset_mm}mm leaves no area inside the "
            "label; use a smaller inset",
            loc=("bleed_guide_inset_mm",),
        )
    return inset_pt


def _draw_label(
    canvas: pdf_canvas.Canvas,
    slot: Slot,
    record: dict[str, Any],
    *,
    record_index: int,
    template: LabelTemplate,
    options: RenderOptions,
    assets: AssetLoader | None,
    label_width_mm: float,
    label_height_mm: float,
    inset_pt: float | None,
    warnings: list[RenderWarning],
) -> None:
    """Draw every element of one label, clipped to its slot."""
    canvas.saveState()
    try:
        # The clip is the guarantee that a label cannot damage its neighbour:
        # an element wider than its slot is cut off at the slot edge instead of
        # printing over the label next to it.
        clip = canvas.beginPath()
        clip.rect(slot.x_pt, slot.y_pt, slot.width_pt, slot.height_pt)
        canvas.clipPath(clip, stroke=0, fill=0)

        if options.outline_slots:
            _draw_slot_outline(canvas, slot)
        if inset_pt is not None:
            _draw_bleed_guide(canvas, slot, inset_pt)

        for element_index, element in enumerate(template.elements):
            _draw_element_guarded(
                canvas,
                slot,
                element,
                record,
                record_index=record_index,
                element_index=element_index,
                options=options,
                assets=assets,
                label_width_mm=label_width_mm,
                label_height_mm=label_height_mm,
                warnings=warnings,
            )
    finally:
        canvas.restoreState()


def _draw_element_guarded(
    canvas: pdf_canvas.Canvas,
    slot: Slot,
    element: TextElement | BarcodeElement | ImageElement,
    record: dict[str, Any],
    *,
    record_index: int,
    element_index: int,
    options: RenderOptions,
    assets: AssetLoader | None,
    label_width_mm: float,
    label_height_mm: float,
    warnings: list[RenderWarning],
) -> None:
    """Draw one element, attributing any failure to its record and element."""
    try:
        _draw_element(
            canvas,
            slot,
            element,
            record,
            options=options,
            assets=assets,
            label_width_mm=label_width_mm,
            label_height_mm=label_height_mm,
        )
    except LabelSheetError as failure:
        if isinstance(failure, RenderError):
            # Mutated rather than rewrapped so a specific subclass, and the
            # HTTP status the API maps it to, survives.
            if failure.record_index is None:
                failure.record_index = record_index
            if failure.element_index is None:
                failure.element_index = element_index
        if options.strict:
            raise
        warnings.append(
            RenderWarning(
                code=failure.code,
                message=failure.message,
                record_index=record_index,
                element_index=element_index,
            )
        )
    except Exception as unexpected:
        # An unexpected exception's text can carry a filesystem path or other
        # internals, and this message reaches the client, so only the type is
        # reported.
        wrapped = RenderError(
            f"{element.type} element could not be rendered ({type(unexpected).__name__})",
            record_index=record_index,
            element_index=element_index,
        )
        if options.strict:
            raise wrapped from unexpected
        warnings.append(
            RenderWarning(
                code=wrapped.code,
                message=wrapped.message,
                record_index=record_index,
                element_index=element_index,
            )
        )


def _draw_element(
    canvas: pdf_canvas.Canvas,
    slot: Slot,
    element: TextElement | BarcodeElement | ImageElement,
    record: dict[str, Any],
    *,
    options: RenderOptions,
    assets: AssetLoader | None,
    label_width_mm: float,
    label_height_mm: float,
) -> None:
    """Dispatch one element to the drawer for its type."""
    if isinstance(element, TextElement):
        draw_text(
            canvas,
            slot,
            element,
            record,
            text_rotation_deg=options.text_rotation_deg,
            label_width_mm=label_width_mm,
            label_height_mm=label_height_mm,
        )
    elif isinstance(element, BarcodeElement):
        draw_barcode(canvas, slot, element, record)
    elif isinstance(element, ImageElement):
        draw_image(canvas, slot, element, record, assets=assets)
    else:  # pragma: no cover - the schema's discriminated union forbids this
        raise RenderError(f"unsupported element type {type(element).__name__}")


def _draw_slot_outline(canvas: pdf_canvas.Canvas, slot: Slot) -> None:
    """Draw the alignment outline for one slot."""
    canvas.saveState()
    try:
        canvas.setStrokeColorRGB(SLOT_OUTLINE_GREY, SLOT_OUTLINE_GREY, SLOT_OUTLINE_GREY)
        canvas.setLineWidth(SLOT_OUTLINE_WIDTH_PT)
        canvas.rect(slot.x_pt, slot.y_pt, slot.width_pt, slot.height_pt, stroke=1, fill=0)
    finally:
        canvas.restoreState()


def _draw_bleed_guide(canvas: pdf_canvas.Canvas, slot: Slot, inset_pt: float) -> None:
    """Draw the dashed safe-area guide inside one slot."""
    canvas.saveState()
    try:
        canvas.setStrokeColorRGB(*BLEED_GUIDE_RGB)
        canvas.setLineWidth(BLEED_GUIDE_WIDTH_PT)
        canvas.setDash(*BLEED_GUIDE_DASH_PT)
        canvas.rect(
            slot.x_pt + inset_pt,
            slot.y_pt + inset_pt,
            slot.width_pt - 2 * inset_pt,
            slot.height_pt - 2 * inset_pt,
            stroke=1,
            fill=0,
        )
    finally:
        canvas.restoreState()
