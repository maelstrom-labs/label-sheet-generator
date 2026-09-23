"""PDF rendering.

:func:`render_pdf` is the entry point: it takes a validated template and a list
of records and returns bytes. :func:`render_page_png` and :func:`page_count`
work on those bytes, so a caller can preview a document without writing it to
disk first.
"""

from __future__ import annotations

from label_sheet_generator.render.pdf import (
    RenderOptions,
    RenderResult,
    RenderWarning,
    render_pdf,
)
from label_sheet_generator.render.preview import page_count, render_page_png

__all__ = [
    "RenderOptions",
    "RenderResult",
    "RenderWarning",
    "page_count",
    "render_page_png",
    "render_pdf",
]
