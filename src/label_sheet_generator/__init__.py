"""Generate print-ready label sheet PDFs from JSON templates and record data."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("label-sheet-generator")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
