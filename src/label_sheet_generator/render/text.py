"""Text drawing, plus the element primitives the other drawers share.

Content substitution goes through :func:`schema.render_template_string` and
never ``str.format`` or ``str.format_map``. The previous implementation
formatted a user-supplied template string against the record, which makes
``{x.__class__.__init__.__globals__}`` a readable expression: a template string
was enough to walk out of the record and into module globals.

The two shared helpers -- :func:`resolve_content` and :func:`element_box_pt` --
live here rather than in :mod:`label_sheet_generator.render.pdf` because
``pdf.py`` imports all three drawers, so defining them there would make an
import cycle. This module imports nothing from the rest of the render package.
"""

from __future__ import annotations

import math
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from label_sheet_generator.errors import (
    GeometryError,
    LabelSheetError,
    RenderError,
    ValidationError,
)
from label_sheet_generator.fonts import ensure_available
from label_sheet_generator.geometry import Slot
from label_sheet_generator.schema import (
    Alignment,
    BarcodeElement,
    ImageElement,
    TextElement,
    render_template_string,
)
from label_sheet_generator.units import check_finite, mm_to_pt

#: Floor for the "shrink" overflow policy. Below about 4pt the glyphs are
#: smaller than the dot pitch of an office laser printer, so shrinking further
#: trades a visible overflow for an unreadable label.
MIN_FONT_SIZE_PT = 4.0

#: Decrement used while searching for a font size that fits. Half a point is
#: the coarsest step that still lands close to the largest size that fits, and
#: the smallest one that changes line breaking often enough to be worth a
#: re-wrap.
FONT_SHRINK_STEP_PT = 0.5

#: Line spacing applied when the element does not declare its own leading.
#: Matches ReportLab's own default ratio.
DEFAULT_LEADING_RATIO = 1.2

#: Appended by the "truncate" policy. Three ASCII periods rather than U+2026
#: because a user-supplied font is not guaranteed to carry the ellipsis glyph,
#: and a missing glyph renders as a black box.
TRUNCATION_SUFFIX = "..."

#: Length of the "#rgb" short hex form, including the leading "#".
_SHORT_HEX_LENGTH = 4

#: Rotation arrives as a float, so quarter turns are matched with a tolerance
#: rather than by equality.
_ANGLE_TOL_DEG = 1e-6

#: Bound on the truncation binary search. 64 steps covers any string the schema
#: admits (8000 characters needs 13); the cap exists so a pathological wrap
#: result cannot spin the loop forever.
_MAX_TRUNCATION_STEPS = 64

_ALIGNMENTS: dict[Alignment, int] = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}

AnyElement = TextElement | BarcodeElement | ImageElement


def resolve_content(
    element: AnyElement,
    record: dict[str, Any],
) -> str | None:
    """Return the string an element draws, or ``None`` when it draws nothing.

    Exactly one of ``template``, ``field`` and ``value`` is populated (the
    schema enforces that). A missing field or a ``None`` value yields ``None``
    so the caller can skip the element entirely; the old code stringified it
    and printed the literal text "None" on the label.
    """
    if element.template is not None:
        return render_template_string(element.template, record)
    if element.field is not None:
        value = record.get(element.field)
        return None if value is None else str(value)
    if element.value is None:
        return None
    return str(element.value)


def element_box_pt(
    slot: Slot,
    element: AnyElement,
    *,
    label_width_mm: float | None = None,
    label_height_mm: float | None = None,
) -> tuple[float, float, float, float]:
    """Return ``(x_pt, y_pt, width_pt, height_pt)`` for an element in a slot.

    Element coordinates are measured from the top-left of the label because
    that is how label stock is described; PDF user space has its origin at the
    bottom-left, so the y axis is flipped here and nowhere else.

    An element that omits a width or height extends to the edge of the label.
    ``label_width_mm`` and ``label_height_mm`` override the slot as the source
    of that extent; they exist so a caller that already resolved the label size
    does not pay a pt->mm->pt round trip.

    Raises:
        GeometryError: if the box has no drawable area.
    """
    x_offset_pt = mm_to_pt(element.x_mm)
    y_offset_pt = mm_to_pt(element.y_mm)

    if element.width_mm is not None:
        width_pt = mm_to_pt(element.width_mm)
    elif label_width_mm is not None:
        width_pt = mm_to_pt(label_width_mm) - x_offset_pt
    else:
        width_pt = slot.width_pt - x_offset_pt

    if element.height_mm is not None:
        height_pt = mm_to_pt(element.height_mm)
    elif label_height_mm is not None:
        height_pt = mm_to_pt(label_height_mm) - y_offset_pt
    else:
        height_pt = slot.height_pt - y_offset_pt

    if width_pt <= 0 or height_pt <= 0:
        raise GeometryError(
            f"{element.type} element at {element.x_mm:.2f}, {element.y_mm:.2f}mm "
            "resolves to a box with no drawable area; move it inside the label "
            "or give it an explicit width and height",
            loc=("elements",),
        )

    x_pt = slot.x_pt + x_offset_pt
    y_pt = slot.y_pt + slot.height_pt - y_offset_pt - height_pt
    return x_pt, y_pt, width_pt, height_pt


def parse_color(value: str) -> Color:
    """Convert a ``#rgb`` or ``#rrggbb`` string to a ReportLab colour.

    Three-digit forms are expanded first: ``HexColor("#abc")`` parses as the
    integer ``0x000abc``, a near-black blue, rather than as ``#aabbcc``.

    Raises:
        ValidationError: if the string is not a hex colour.
    """
    text = value.strip()
    if text.startswith("#") and len(text) == _SHORT_HEX_LENGTH:
        text = "#" + "".join(character * 2 for character in text[1:])
    try:
        return HexColor(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"unsupported colour value {value!r}; use #rgb or #rrggbb") from exc


def draw_text(
    canvas: Canvas,
    slot: Slot,
    element: TextElement,
    record: dict[str, Any],
    *,
    text_rotation_deg: float | None,
    label_width_mm: float,
    label_height_mm: float,
) -> None:
    """Draw one text element into its slot, honouring overflow and rotation.

    Raises:
        RenderError: if the text cannot be laid out, or overflows under the
            "error" policy.
        ValidationError: if the font or colour cannot be resolved.
        GeometryError: if the element box has no area.
    """
    text = resolve_content(element, record)
    if not text:
        return

    x_pt, y_pt, width_pt, height_pt = element_box_pt(
        slot,
        element,
        label_width_mm=label_width_mm,
        label_height_mm=label_height_mm,
    )
    font_name = ensure_available(element.font_name, loc=("font_name",))
    rotation_deg = _effective_rotation_deg(element, text_rotation_deg)

    # A quarter turn swaps which box dimension bounds the line length.
    quarter_turn = math.isclose(rotation_deg % 180.0, 90.0, abs_tol=_ANGLE_TOL_DEG)
    layout_width_pt = height_pt if quarter_turn else width_pt
    layout_height_pt = width_pt if quarter_turn else height_pt

    paragraph, rendered_height_pt = _fit_to_box(
        text, element, font_name, layout_width_pt, layout_height_pt
    )
    # Deliberately not clamped to zero: when the text is taller than its box
    # the surplus has to go somewhere, and it should go away from the edge the
    # author anchored to, so top-aligned text keeps its first line visible and
    # loses the tail to the clip below.
    free_pt = layout_height_pt - rendered_height_pt
    if element.valign == "top":
        offset_pt = free_pt
    elif element.valign == "middle":
        offset_pt = free_pt / 2.0
    else:
        offset_pt = 0.0

    canvas.saveState()
    try:
        # Rotate about the centre of the box so the text stays in its box at
        # any angle, then work in box-local coordinates.
        canvas.translate(x_pt + width_pt / 2.0, y_pt + height_pt / 2.0)
        if not math.isclose(rotation_deg, 0.0, abs_tol=_ANGLE_TOL_DEG):
            canvas.rotate(rotation_deg)
        clip = canvas.beginPath()
        clip.rect(
            -layout_width_pt / 2.0,
            -layout_height_pt / 2.0,
            layout_width_pt,
            layout_height_pt,
        )
        # Every policy clips: "shrink" can bottom out at MIN_FONT_SIZE_PT and
        # still not fit, and unclipped text climbs out of the top of its box
        # and over whatever is above it, which is the old defect.
        canvas.clipPath(clip, stroke=0, fill=0)
        paragraph.drawOn(canvas, -layout_width_pt / 2.0, -layout_height_pt / 2.0 + offset_pt)
    except LabelSheetError:
        raise
    except Exception as exc:  # ReportLab raises assorted builtins from drawOn.
        raise RenderError(f"text element could not be drawn ({type(exc).__name__})") from exc
    finally:
        canvas.restoreState()


def _effective_rotation_deg(element: TextElement, text_rotation_deg: float | None) -> float:
    """Resolve the element rotation against the sheet-wide override."""
    if text_rotation_deg is None:
        return element.rotation_deg % 360.0
    try:
        override = check_finite(float(text_rotation_deg), "text_rotation_deg")
    except (TypeError, ValueError) as exc:
        raise ValidationError("text_rotation_deg must be a finite number") from exc
    return override % 360.0


def _build_paragraph(
    text: str,
    element: TextElement,
    font_name: str,
    font_size_pt: float,
) -> Paragraph:
    """Build a Paragraph at a given size, escaping the text for ReportLab."""
    if element.leading_pt is not None:
        # Scale the authored leading with the font so a shrunk paragraph keeps
        # the spacing the author chose rather than growing gaps between lines.
        leading_pt = element.leading_pt * (font_size_pt / element.font_size_pt)
    else:
        leading_pt = font_size_pt * DEFAULT_LEADING_RATIO

    style = ParagraphStyle(
        name="label-text",
        fontName=font_name,
        fontSize=font_size_pt,
        leading=leading_pt,
        textColor=parse_color(element.color),
        alignment=_ALIGNMENTS[element.align],
    )
    # Paragraph parses its input as mini-XML, so any "<" or "&" in record data
    # is markup unless escaped. Newlines survive as explicit line breaks.
    markup = escape(text).replace("\n", "<br/>")
    try:
        return Paragraph(markup, style)
    except LabelSheetError:
        raise
    except Exception as exc:
        raise RenderError(f"text could not be laid out ({type(exc).__name__})") from exc


def _wrapped_height_pt(paragraph: Paragraph, width_pt: float, height_pt: float) -> float:
    """Return the height the paragraph needs at the given line width."""
    try:
        _, rendered_height_pt = paragraph.wrap(width_pt, height_pt)
    except LabelSheetError:
        raise
    except Exception as exc:
        raise RenderError(f"text could not be wrapped ({type(exc).__name__})") from exc
    return float(rendered_height_pt)


def _fit_to_box(
    text: str,
    element: TextElement,
    font_name: str,
    layout_width_pt: float,
    layout_height_pt: float,
) -> tuple[Paragraph, float]:
    """Apply the element's overflow policy, returning the paragraph to draw."""
    paragraph = _build_paragraph(text, element, font_name, element.font_size_pt)
    rendered_height_pt = _wrapped_height_pt(paragraph, layout_width_pt, layout_height_pt)
    if rendered_height_pt <= layout_height_pt or element.overflow == "clip":
        return paragraph, rendered_height_pt

    if element.overflow == "error":
        raise RenderError(
            f"text needs {rendered_height_pt:.1f}pt of height but its box is only "
            f"{layout_height_pt:.1f}pt; shorten the text, enlarge the box, or use "
            "an overflow policy of shrink, truncate or clip"
        )

    if element.overflow == "shrink":
        font_size_pt = element.font_size_pt
        while font_size_pt - FONT_SHRINK_STEP_PT >= MIN_FONT_SIZE_PT:
            font_size_pt -= FONT_SHRINK_STEP_PT
            paragraph = _build_paragraph(text, element, font_name, font_size_pt)
            rendered_height_pt = _wrapped_height_pt(paragraph, layout_width_pt, layout_height_pt)
            if rendered_height_pt <= layout_height_pt:
                break
        return paragraph, rendered_height_pt

    return _truncate_to_box(text, element, font_name, layout_width_pt, layout_height_pt)


def _truncate_to_box(
    text: str,
    element: TextElement,
    font_name: str,
    layout_width_pt: float,
    layout_height_pt: float,
) -> tuple[Paragraph, float]:
    """Return the longest prefix of ``text`` that fits, marked as truncated.

    Binary search rather than a character-at-a-time walk: wrapping is the
    expensive part, and a long value would otherwise re-wrap thousands of
    times.
    """
    best: tuple[Paragraph, float] | None = None
    low, high = 0, len(text)
    steps = 0

    while low <= high and steps < _MAX_TRUNCATION_STEPS:
        steps += 1
        cut = (low + high) // 2
        candidate = text[:cut].rstrip() + TRUNCATION_SUFFIX
        paragraph = _build_paragraph(candidate, element, font_name, element.font_size_pt)
        rendered_height_pt = _wrapped_height_pt(paragraph, layout_width_pt, layout_height_pt)
        if rendered_height_pt <= layout_height_pt:
            best = (paragraph, rendered_height_pt)
            low = cut + 1
        else:
            high = cut - 1

    if best is None:
        # Not even the marker fits. Draw it anyway so the label shows that
        # something was dropped; the caller's clip keeps it inside the box.
        paragraph = _build_paragraph(TRUNCATION_SUFFIX, element, font_name, element.font_size_pt)
        return paragraph, _wrapped_height_pt(paragraph, layout_width_pt, layout_height_pt)
    return best
