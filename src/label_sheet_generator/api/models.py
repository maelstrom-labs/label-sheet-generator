"""Request and response models for the HTTP API.

Every model sets ``extra='forbid'`` and ``allow_inf_nan=False``, and every
string field has a ``max_length``. That combination is what turns a hostile
body into a 422 with a field-level explanation instead of a 500 from somewhere
deep in the renderer.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Generous enough for a real filename, short enough that it cannot be used to
#: pad a response header.
MAX_FILENAME_LENGTH = 120
MAX_ID_LENGTH = 200
#: The service enforces the real document cap by byte count; this is a cheap
#: upper bound so an enormous string is rejected before it is parsed.
MAX_DOCUMENT_CHARS = 4_000_000

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)

Millimetres = Annotated[float, Field(ge=0, le=5_000)]


class MarginOverridesModel(BaseModel):
    model_config = _STRICT

    margin_top_mm: Millimetres | None = None
    margin_right_mm: Millimetres | None = None
    margin_bottom_mm: Millimetres | None = None
    margin_left_mm: Millimetres | None = None


class RenderRequest(BaseModel):
    """The one request shape shared by plan, preview and pdf."""

    model_config = _STRICT

    template_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    layout_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    document: str = Field(default="", max_length=MAX_DOCUMENT_CHARS)
    overrides: MarginOverridesModel = Field(default_factory=MarginOverridesModel)
    page_orientation: Literal["portrait", "landscape"] = "portrait"
    page_rotation_deg: Literal[0, 90, 180, 270] = 0
    text_rotation_deg: Annotated[float, Field(ge=-360, le=360)] | None = None
    outline_slots: bool = True
    bleed_guide_inset_mm: Annotated[float, Field(ge=0, le=100)] | None = None
    filename: str | None = Field(default=None, max_length=MAX_FILENAME_LENGTH)


class PreviewRequest(RenderRequest):
    page: Annotated[int, Field(ge=0, le=10_000)] = 0
    scale: Annotated[float, Field(gt=0, le=8.0)] | None = None


class ValidateTemplateRequest(BaseModel):
    model_config = _STRICT

    template: dict[str, Any]


class ValidateRecordsRequest(BaseModel):
    model_config = _STRICT

    template_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    layout_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    document: str = Field(default="", max_length=MAX_DOCUMENT_CHARS)


class HealthResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    version: str
    python: str
