"""PNG previews of a rendered PDF, straight from bytes.

Everything here works on the in-memory PDF produced by
:func:`label_sheet_generator.render.pdf.render_pdf`. The old preview path
wrote the PDF to a temporary file, handed the path to pypdfium2 and read it
back, which leaked a file per request when rendering raised, and made two
concurrent previews of the same document race for the same name.

pypdfium2 is an optional dependency: the library renders PDFs without it, and
only the preview endpoint needs it, so the import is deferred and a missing
install is a configuration error rather than an import-time crash.
"""

from __future__ import annotations

import io
import math
from typing import Any

from label_sheet_generator.errors import (
    ConfigurationError,
    LimitExceeded,
    ValidationError,
)

#: pypdfium2 renders at ``scale`` pixels per PostScript point, so scale 1.0 is
#: 72dpi. This bounds how far a caller may push that.
MIN_SCALE = 0.01
MAX_SCALE = 10.0

#: Install hint used whenever the optional preview dependency is missing.
_INSTALL_HINT = "install the preview extra: pip install 'label-sheet-generator[preview]'"


def page_count(pdf_bytes: bytes) -> int:
    """Return the number of pages in a PDF.

    Raises:
        ValidationError: if the bytes are not a readable PDF.
        ConfigurationError: if pypdfium2 is not installed.
    """
    pdfium = _import_pdfium()
    document = _open_document(pdfium, pdf_bytes)
    try:
        return len(document)
    finally:
        document.close()


def render_page_png(
    pdf_bytes: bytes,
    *,
    page: int = 0,
    scale: float = 1.5,
    max_pixels: int = 16_000_000,
) -> bytes:
    """Rasterise one page of a PDF to PNG bytes.

    ``page`` is clamped to the last page, so a caller paging past the end of a
    shortened document gets the last page rather than an error.

    Raises:
        ValidationError: for a negative page, an out-of-range scale, or bytes
            that are not a readable PDF.
        LimitExceeded: if the requested scale would exceed ``max_pixels``.
        ConfigurationError: if pypdfium2 or Pillow is not installed.
    """
    if page < 0:
        raise ValidationError(f"page must be zero or greater, got {page}", loc=("page",))
    if max_pixels < 1:
        raise ValidationError("max_pixels must be at least 1", loc=("max_pixels",))
    if not math.isfinite(scale) or not MIN_SCALE <= scale <= MAX_SCALE:
        raise ValidationError(
            f"scale must be between {MIN_SCALE} and {MAX_SCALE}, got {scale}",
            loc=("scale",),
        )

    pdfium = _import_pdfium()
    document = _open_document(pdfium, pdf_bytes)
    try:
        total = len(document)
        if total == 0:
            raise ValidationError("the document has no pages")
        index = min(page, total - 1)

        width_pt, height_pt = document.get_page_size(index)
        pixels = math.ceil(width_pt * scale) * math.ceil(height_pt * scale)
        if pixels > max_pixels:
            raise LimitExceeded(
                f"rendering page {index + 1} at scale {scale} needs {pixels} pixels, "
                f"which is over the {max_pixels} pixel cap; request a smaller scale",
                limit_name="max_pixels",
                limit=max_pixels,
                actual=pixels,
                loc=("scale",),
            )
        return _rasterise(document, index, scale)
    finally:
        document.close()


def _rasterise(document: Any, index: int, scale: float) -> bytes:
    """Render one already-validated page index to PNG bytes."""
    page_handle = document[index]
    try:
        bitmap = page_handle.render(scale=scale)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        raise ValidationError(f"page {index + 1} could not be rendered: {exc}") from exc
    finally:
        # pdfium page handles are C resources; without this they are only
        # released when the document is closed, and a batch of previews holds
        # every page bitmap alive at once.
        page_handle.close()


def _import_pdfium() -> Any:
    """Import pypdfium2, or explain how to install it.

    Pillow is imported alongside it because the PNG encoding below goes
    through it, and failing here names both dependencies at once instead of
    failing again halfway through a render.
    """
    try:
        import PIL.Image  # noqa: F401, PLC0415 - presence check for the encoder
        import pypdfium2  # noqa: PLC0415 - optional dependency, imported on use
    except ImportError as exc:
        raise ConfigurationError(
            f"PDF preview requires pypdfium2 and Pillow; {_INSTALL_HINT}"
        ) from exc
    return pypdfium2


def _open_document(pdfium: Any, pdf_bytes: bytes) -> Any:
    """Open a PDF from bytes, mapping every failure to a domain error."""
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise ValidationError("pdf_bytes must be bytes")
    if not pdf_bytes:
        raise ValidationError("pdf_bytes is empty")

    try:
        return pdfium.PdfDocument(bytes(pdf_bytes))
    except Exception as exc:
        # Includes PdfiumError for a corrupt or password-protected file.
        raise ValidationError(f"the bytes are not a readable PDF: {exc}") from exc
