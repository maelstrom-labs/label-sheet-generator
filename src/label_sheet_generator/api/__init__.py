"""HTTP layer. Nothing below this package imports FastAPI."""

from __future__ import annotations

from label_sheet_generator.api.app import create_app

__all__ = ["create_app"]
