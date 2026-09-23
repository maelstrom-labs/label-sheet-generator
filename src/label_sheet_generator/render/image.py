"""Image drawing.

Image references are the highest-risk input this package takes: a reference is
a name that becomes a file read. Nothing in this module resolves one. It asks
:class:`label_sheet_generator.assets.AssetLoader`, which is the only component
allowed to turn a reference into bytes, and which is off unless an asset root
has been configured. The old code called ``Path(raw)`` directly, honoured
absolute paths, and probed directories next to the template and record files,
so a record value could read any file the process could.
"""

from __future__ import annotations

from typing import Any

from reportlab.pdfgen.canvas import Canvas

from label_sheet_generator.assets import AssetLoader
from label_sheet_generator.errors import AssetError, LabelSheetError, RenderError
from label_sheet_generator.geometry import Slot
from label_sheet_generator.render.text import element_box_pt, resolve_content
from label_sheet_generator.schema import ImageElement


def draw_image(
    canvas: Canvas,
    slot: Slot,
    element: ImageElement,
    record: dict[str, Any],
    *,
    assets: AssetLoader | None,
) -> None:
    """Draw one image element, scaled by its fit policy and aligned in its box.

    Raises:
        AssetError: if image support is disabled, or the reference cannot be
            resolved to a readable image.
        RenderError: if the image cannot be drawn.
        GeometryError: if the element box has no area.
    """
    reference = resolve_content(element, record)
    if not reference:
        return

    if assets is None or not assets.enabled:
        raise AssetError(
            "image elements are disabled because no asset root is configured; "
            "start the server with an asset root to enable them"
        )

    image = assets.load(reference)
    source_width_px, source_height_px = _source_size(image)
    x_pt, y_pt, box_width_pt, box_height_pt = element_box_pt(slot, element)

    draw_width_pt, draw_height_pt = _fitted_size_pt(
        element.fit,
        source_width_px,
        source_height_px,
        box_width_pt,
        box_height_pt,
    )
    draw_x_pt = x_pt + _axis_offset_pt(box_width_pt, draw_width_pt, _horizontal_fraction(element))
    draw_y_pt = y_pt + _axis_offset_pt(box_height_pt, draw_height_pt, _vertical_fraction(element))

    canvas.saveState()
    try:
        if element.fit == "cover":
            # "cover" fills the box by overflowing on one axis; without a clip
            # that overflow lands on the neighbouring element.
            clip = canvas.beginPath()
            clip.rect(x_pt, y_pt, box_width_pt, box_height_pt)
            canvas.clipPath(clip, stroke=0, fill=0)
        canvas.drawImage(
            image,
            draw_x_pt,
            draw_y_pt,
            width=draw_width_pt,
            height=draw_height_pt,
            preserveAspectRatio=False,
            mask="auto",
        )
    except LabelSheetError:
        raise
    except Exception as exc:
        # The message deliberately omits the reference: it is echoed back to
        # the client, and asset errors are the ones most likely to be probed.
        raise RenderError(f"image could not be drawn ({type(exc).__name__})") from exc
    finally:
        canvas.restoreState()


def _source_size(image: Any) -> tuple[float, float]:
    """Return the pixel size of a loaded image."""
    try:
        width_px, height_px = image.getSize()
    except LabelSheetError:
        raise
    except Exception as exc:
        raise AssetError(f"image dimensions could not be read ({type(exc).__name__})") from exc

    if width_px <= 0 or height_px <= 0:
        raise AssetError("image has no pixels")
    return float(width_px), float(height_px)


def _fitted_size_pt(
    fit: str,
    source_width_px: float,
    source_height_px: float,
    box_width_pt: float,
    box_height_pt: float,
) -> tuple[float, float]:
    """Return the drawn size for a fit policy."""
    if fit == "stretch":
        return box_width_pt, box_height_pt

    width_ratio = box_width_pt / source_width_px
    height_ratio = box_height_pt / source_height_px
    # "contain" fits inside the box, "cover" fills it and overflows.
    scale = max(width_ratio, height_ratio) if fit == "cover" else min(width_ratio, height_ratio)
    return source_width_px * scale, source_height_px * scale


def _axis_offset_pt(box_pt: float, drawn_pt: float, fraction: float) -> float:
    """Return the offset that places a drawn extent within a box extent."""
    return (box_pt - drawn_pt) * fraction


def _horizontal_fraction(element: ImageElement) -> float:
    """Where along the box width the image sits: 0 left, 0.5 centre, 1 right."""
    if element.align == "center":
        return 0.5
    if element.align == "right":
        return 1.0
    # "justify" has no meaning for a single image; treat it as left, as the
    # schema shares one alignment enum across element types.
    return 0.0


def _vertical_fraction(element: ImageElement) -> float:
    """Where along the box height the image sits, measured from the bottom."""
    if element.valign == "middle":
        return 0.5
    if element.valign == "top":
        return 1.0
    return 0.0
