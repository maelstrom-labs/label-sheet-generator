"""Barcode drawing for Code 128, EAN-13 and QR.

The EAN-13 handling is the reason this module exists separately. The previous
implementation took a 13-digit input, sliced it to ``digits[:12]`` and let
ReportLab recompute the check digit. A mistyped check digit therefore produced
a different, perfectly valid, perfectly scannable barcode -- the one failure
mode a barcode must not have, because nothing downstream can detect it. Here
the check digit is verified, and a mismatch is an error that names the digit
the payload should have ended with.
"""

from __future__ import annotations

from typing import Any

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics.shapes import Drawing
from reportlab.pdfgen.canvas import Canvas

from label_sheet_generator.errors import LabelSheetError, RenderError
from label_sheet_generator.geometry import Slot
from label_sheet_generator.render.text import element_box_pt, resolve_content
from label_sheet_generator.schema import BarcodeElement, BarcodeType
from label_sheet_generator.units import pt_to_mm

#: Narrowest bar (or QR module) that still scans, in points. 0.5pt is 0.18mm;
#: the GS1 minimum X-dimension for retail symbols is 0.19mm, and consumer
#: scanners start failing below roughly that. Emitting a smaller symbol is
#: worse than emitting none: it looks correct and cannot be read.
MIN_MODULE_WIDTH_PT = 0.5

#: Modules of white space ReportLab leaves around a QR symbol. The QR spec
#: requires 4; 0 is only safe when the label itself provides the margin.
QR_QUIET_MODULES = 4

#: Digits in a full EAN-13 payload, and in the body that precedes its check
#: digit.
EAN13_LENGTH = 13
EAN13_BODY_LENGTH = 12

#: EAN-13 weights its digits 1, 3, 1, 3, ... from the left.
_EAN13_ODD_WEIGHT = 1
_EAN13_EVEN_WEIGHT = 3

#: Characters that are visual separators in a printed GTIN rather than data.
_EAN13_SEPARATORS = " -\t"

_BARCODE_KINDS: dict[BarcodeType, str] = {
    "code128": "Code128",
    "ean13": "EAN13",
    "qr": "QR",
}


def ean13_check_digit(twelve_digits: str) -> str:
    """Return the EAN-13 check digit for a 12-digit body.

    Raises:
        RenderError: if the body is not exactly 12 digits.
    """
    if len(twelve_digits) != EAN13_BODY_LENGTH or not twelve_digits.isdigit():
        raise RenderError(
            f"an EAN-13 check digit needs exactly {EAN13_BODY_LENGTH} digits, "
            f"got {len(twelve_digits)} character(s)"
        )

    total = 0
    for position, character in enumerate(twelve_digits):
        weight = _EAN13_EVEN_WEIGHT if position % 2 else _EAN13_ODD_WEIGHT
        total += int(character) * weight
    return str((10 - total % 10) % 10)


def draw_barcode(
    canvas: Canvas,
    slot: Slot,
    element: BarcodeElement,
    record: dict[str, Any],
) -> None:
    """Draw one barcode element, centred in its box.

    Raises:
        RenderError: if the payload is not encodable, or the box is too small
            for the symbol to scan.
        GeometryError: if the element box has no area.
    """
    content = resolve_content(element, record)
    if not content:
        return

    payload = _normalize_payload(element.barcode_type, content)
    x_pt, y_pt, width_pt, height_pt = element_box_pt(slot, element)
    drawing = _create_drawing(element, payload)

    if drawing.width <= 0 or drawing.height <= 0:
        raise RenderError(f"the {element.barcode_type} symbol has no drawable size")

    # Uniform scale: a barcode stretched on one axis only distorts its module
    # widths, which is what a scanner measures.
    scale = min(width_pt / drawing.width, height_pt / drawing.height)
    module_width_pt = _module_width_pt(drawing, element.barcode_type)
    if module_width_pt is not None and module_width_pt * scale < MIN_MODULE_WIDTH_PT:
        raise RenderError(
            f"a {element.barcode_type} symbol does not fit in a "
            f"{pt_to_mm(width_pt):.1f} x {pt_to_mm(height_pt):.1f}mm box at a "
            "scannable size; enlarge the element, shorten the payload, or turn "
            "off the quiet zone"
        )

    draw_x_pt = x_pt + (width_pt - drawing.width * scale) / 2.0
    draw_y_pt = y_pt + (height_pt - drawing.height * scale) / 2.0

    canvas.saveState()
    try:
        canvas.translate(draw_x_pt, draw_y_pt)
        canvas.scale(scale, scale)
        renderPDF.draw(drawing, canvas, 0, 0)
    except LabelSheetError:
        raise
    except Exception as exc:
        raise RenderError(
            f"{element.barcode_type} symbol could not be drawn ({type(exc).__name__})"
        ) from exc
    finally:
        canvas.restoreState()


def _normalize_payload(barcode_type: BarcodeType, content: str) -> str:
    """Return the payload to encode, validating it for the symbology."""
    if barcode_type != "ean13":
        return content

    digits = "".join(c for c in content if c not in _EAN13_SEPARATORS)
    if not digits.isdigit():
        raise RenderError(
            f"EAN-13 payload {content!r} contains non-digit characters; "
            "only digits, spaces and hyphens are accepted"
        )

    if len(digits) == EAN13_BODY_LENGTH:
        return digits + ean13_check_digit(digits)

    if len(digits) == EAN13_LENGTH:
        expected = ean13_check_digit(digits[:EAN13_BODY_LENGTH])
        if digits[-1] != expected:
            raise RenderError(
                f"EAN-13 payload {digits} has check digit {digits[-1]} but "
                f"{digits[:EAN13_BODY_LENGTH]} requires {expected}; correct the "
                "payload or supply the first 12 digits and let it be computed"
            )
        return digits

    raise RenderError(
        f"EAN-13 needs {EAN13_BODY_LENGTH} digits, or {EAN13_LENGTH} including "
        f"the check digit; got {len(digits)}"
    )


def _create_drawing(element: BarcodeElement, payload: str) -> Drawing:
    """Build the ReportLab drawing for an element's payload."""
    kind = _BARCODE_KINDS[element.barcode_type]
    options: dict[str, Any] = {"value": payload, "humanReadable": element.human_readable}
    if element.barcode_type == "qr":
        options["barBorder"] = QR_QUIET_MODULES if element.quiet_zone else 0
    else:
        options["quiet"] = element.quiet_zone

    try:
        return createBarcodeDrawing(kind, **options)
    except LabelSheetError:
        raise
    except Exception as exc:
        # ReportLab signals an unencodable payload with an AttributeError from
        # its attribute-map validation, so the exception type says nothing
        # useful to a caller; the payload and symbology do.
        raise RenderError(f"value cannot be encoded as {element.barcode_type}: {exc}") from exc


def _module_width_pt(drawing: Drawing, barcode_type: BarcodeType) -> float | None:
    """Return the unscaled narrow-bar width, or ``None`` if it is unknown."""
    widget = getattr(drawing, "_bc", None)
    if widget is None:
        return None

    if barcode_type == "qr":
        # QrCodeWidget.barWidth is the width of the whole symbol, not of one
        # module, so the module size has to come from the matrix.
        matrix = getattr(getattr(widget, "qr", None), "modules", None)
        if not matrix:
            return None
        span = len(matrix) + 2 * int(getattr(widget, "barBorder", 0) or 0)
        return drawing.width / span if span > 0 else None

    bar_width = getattr(widget, "barWidth", None)
    if not isinstance(bar_width, (int, float)) or bar_width <= 0:
        return None
    return float(bar_width)
