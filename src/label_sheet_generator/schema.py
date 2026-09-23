"""Pydantic v2 models for templates, elements, and record documents.

This module is the trust boundary. Anything that has been through a model here
is known to be structurally valid, finite, positively-sized, and free of
unknown keys -- so no code downstream needs defensive checks.

Three design decisions worth stating, because each fixes a specific defect in
the previous hand-rolled dataclass layer:

* **Discriminated unions, not inheritance with a mutable ``type`` attribute.**
  The old ``@dataclass(slots=True)`` hierarchy called zero-argument ``super()``
  from a subclass method. ``slots=True`` cannot mutate a class in place, so the
  decorator returns a *new* class while the ``__class__`` cell captured by
  ``super()`` still refers to the pre-decoration one, and every ``to_dict()``
  raised ``TypeError``. Here ``type`` is a ``Literal`` discriminator and
  serialisation is ``model_dump``, so the failure mode cannot recur.

* **Lengths are authored in mm or in, stored in mm, written back in the unit
  they arrived in.** ``width_mm`` and ``width_in`` are mutually exclusive
  aliases for one value. The authored unit is recorded on the document so a
  file authored in inches round-trips as inches.

* **``extra='forbid'`` and ``allow_inf_nan=False`` everywhere.** A typo'd key
  is an error rather than a silently ignored no-op, and NaN cannot enter the
  geometry pipeline, where it defeats every bounds check by comparing False.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from label_sheet_generator.units import MM_PER_INCH, quantize

Unit = Literal["mm", "in"]
Alignment = Literal["left", "center", "right", "justify"]
VerticalAlignment = Literal["top", "middle", "bottom"]
BarcodeType = Literal["code128", "ean13", "qr"]
ImageFit = Literal["contain", "cover", "stretch"]
OverflowPolicy = Literal["shrink", "clip", "truncate", "error"]

#: Hard structural caps. These bound the work a single document can request
#: before any rendering starts; the per-request limits in settings.py bound it
#: again at the HTTP boundary.
MAX_ELEMENTS = 200
MAX_FIELD_NAME_LENGTH = 128
MAX_TEXT_LENGTH = 8_000
MAX_NAME_LENGTH = 200
MAX_GRID_CELLS = 10_000

#: A field reference inside a ``template`` string, e.g. ``{address_1}``.
#: Deliberately narrow: no attribute access, no indexing, no conversion or
#: format specs, no auto-numbering. See :func:`parse_field_references`.
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

_BASE_CONFIG = ConfigDict(
    extra="forbid",
    allow_inf_nan=False,
    frozen=True,
    str_strip_whitespace=False,
    validate_default=True,
)


class _Base(BaseModel):
    model_config = _BASE_CONFIG


# --------------------------------------------------------------------------
# Dual-unit length handling
# --------------------------------------------------------------------------


def _resolve_lengths(data: Any, names: tuple[str, ...], section: str) -> Any:
    """Collapse ``<name>_mm`` / ``<name>_in`` pairs into millimetres.

    Records which unit was used under the private ``_authored_unit`` key so the
    enclosing document can serialise back in the unit it was written in.
    """
    if not isinstance(data, dict):
        return data

    out = dict(data)
    seen_units: set[Unit] = set()

    for name in names:
        mm_key, in_key = f"{name}_mm", f"{name}_in"
        has_mm = out.get(mm_key) is not None
        has_in = out.get(in_key) is not None

        if has_mm and has_in:
            raise ValueError(f"{section}.{name} accepts either {mm_key} or {in_key}, not both")
        if has_in:
            value = out.pop(in_key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{section}.{in_key} must be a number")
            out[mm_key] = float(value) * MM_PER_INCH
            seen_units.add("in")
        else:
            out.pop(in_key, None)
            if has_mm:
                seen_units.add("mm")

    if seen_units:
        out.setdefault("_authored_unit", "in" if "in" in seen_units else "mm")
    return out


def _dump_lengths(payload: dict[str, Any], names: tuple[str, ...], unit: Unit) -> dict[str, Any]:
    """Rewrite millimetre keys back into the authored unit, quantized."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        base = key[:-3] if key.endswith("_mm") else None
        if base is not None and base in names and isinstance(value, (int, float)):
            if unit == "in":
                out[f"{base}_in"] = quantize(float(value) / MM_PER_INCH)
            else:
                out[f"{base}_mm"] = quantize(float(value))
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# Template strings
# --------------------------------------------------------------------------


def parse_field_references(text: str, *, loc: str = "template") -> list[str]:
    """Validate a ``{field}`` template string and return the names it uses.

    ``str.format`` is never used on these. ``"{a.__class__}"`` and
    ``"{0.__globals__}"`` are attribute walks that reach module internals from
    a user-supplied string, so the grammar here rejects everything except a
    bare identifier, and substitution is a plain regex replace.

    ``{{`` and ``}}`` are literal braces, as in ``str.format``.
    """
    scrubbed = text.replace("{{", "\x00").replace("}}", "\x01")
    if "{" in _PLACEHOLDER_RE.sub("", scrubbed) or "}" in _PLACEHOLDER_RE.sub("", scrubbed):
        raise ValueError(f"{loc} has an unmatched brace; use {{{{ and }}}} for a literal brace")

    names: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(scrubbed):
        name = match.group(1)
        if name == "":
            raise ValueError(f"{loc} uses {{}}; name the field explicitly, e.g. {{name}}")
        if not FIELD_NAME_RE.match(name):
            raise ValueError(
                f"{loc} reference {{{name}}} is not a plain field name; "
                "attribute access, indexing, conversions and format specs are not supported"
            )
        if name not in names:
            names.append(name)
    return names


def render_template_string(text: str, record: dict[str, Any]) -> str:
    """Substitute ``{field}`` references from ``record``; unknown fields blank."""

    def replace(match: re.Match[str]) -> str:
        value = record.get(match.group(1))
        return "" if value is None else str(value)

    scrubbed = text.replace("{{", "\x00").replace("}}", "\x01")
    out = _PLACEHOLDER_RE.sub(replace, scrubbed)
    return out.replace("\x00", "{").replace("\x01", "}")


# --------------------------------------------------------------------------
# Elements
# --------------------------------------------------------------------------

_ELEMENT_LENGTHS = ("x", "y", "width", "height")

Length = Annotated[float, Field(ge=0, le=10_000)]
OptionalLength = Annotated[float | None, Field(default=None, gt=0, le=10_000)]


class _ElementBase(_Base):
    if TYPE_CHECKING:
        # Every concrete subclass declares `type` as a Literal discriminator.
        # Declaring it here is for the type checker only: making it a real
        # field on the base would give pydantic an extra inherited field to
        # reconcile against each subclass's Literal.
        type: str

    x_mm: Length
    y_mm: Length
    width_mm: OptionalLength = None
    height_mm: OptionalLength = None
    field: str | None = Field(default=None, max_length=MAX_FIELD_NAME_LENGTH)
    value: str | int | float | bool | None = None
    template: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    authored_unit: Unit = Field(default="mm", alias="_authored_unit", exclude=True)

    model_config = ConfigDict(**{**_BASE_CONFIG, "populate_by_name": True})

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        return _resolve_lengths(data, _ELEMENT_LENGTHS, "element")

    @field_validator("field")
    @classmethod
    def _check_field_name(cls, value: str | None) -> str | None:
        if value is not None and not FIELD_NAME_RE.match(value):
            raise ValueError(
                f"field name {value!r} must start with a letter or underscore "
                "and contain only letters, digits and underscores"
            )
        return value

    @field_validator("template")
    @classmethod
    def _check_template(cls, value: str | None) -> str | None:
        if value is not None:
            parse_field_references(value, loc="element.template")
        return value

    @model_validator(mode="after")
    def _check_source(self) -> _ElementBase:
        provided = [n for n in ("field", "value", "template") if getattr(self, n) is not None]
        if len(provided) > 1:
            raise ValueError(
                f"element declares {' and '.join(provided)}; choose exactly one content source"
            )
        return self

    @property
    def field_names(self) -> list[str]:
        """Record fields this element reads."""
        if self.template is not None:
            return parse_field_references(self.template)
        return [self.field] if self.field else []

    def dump(self, unit: Unit) -> dict[str, Any]:
        """Serialise, omitting anything left at its default.

        Keeping defaults out makes a written template readable and diffable --
        the file says what the author chose, not what the schema happens to
        default to. ``type`` is forced back in because it is the discriminator
        and a document without it cannot be parsed.
        """
        payload = self.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True, by_alias=False
        )
        payload = {"type": self.type, **payload}
        return _dump_lengths(payload, _ELEMENT_LENGTHS, unit)


class TextElement(_ElementBase):
    type: Literal["text"] = "text"
    font_name: str = Field(default="Helvetica", max_length=100)
    font_size_pt: float = Field(default=10.0, gt=0, le=1_000)
    leading_pt: float | None = Field(default=None, gt=0, le=1_000)
    color: str = Field(default="#000000", pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    align: Alignment = "left"
    valign: VerticalAlignment = "top"
    rotation_deg: float = Field(default=0.0, ge=-360, le=360)
    overflow: OverflowPolicy = "shrink"


class BarcodeElement(_ElementBase):
    type: Literal["barcode"] = "barcode"
    barcode_type: BarcodeType = "code128"
    human_readable: bool = False
    quiet_zone: bool = True

    @model_validator(mode="after")
    def _require_box(self) -> BarcodeElement:
        if self.width_mm is None or self.height_mm is None:
            raise ValueError("barcode elements require a width and a height")
        return self


class ImageElement(_ElementBase):
    type: Literal["image"] = "image"
    fit: ImageFit = "contain"
    align: Alignment = "left"
    valign: VerticalAlignment = "top"

    @model_validator(mode="after")
    def _require_box(self) -> ImageElement:
        if self.width_mm is None or self.height_mm is None:
            raise ValueError("image elements require a width and a height")
        return self


Element = Annotated[
    TextElement | BarcodeElement | ImageElement,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Page and grid
# --------------------------------------------------------------------------


class PageSpec(_Base):
    width_mm: Annotated[float, Field(gt=0, le=10_000)]
    height_mm: Annotated[float, Field(gt=0, le=10_000)]
    authored_unit: Unit = Field(default="mm", alias="_authored_unit", exclude=True)

    model_config = ConfigDict(**{**_BASE_CONFIG, "populate_by_name": True})

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        return _resolve_lengths(data, ("width", "height"), "page")

    def dump(self, unit: Unit) -> dict[str, Any]:
        return _dump_lengths(self.model_dump(mode="json"), ("width", "height"), unit)

    # PageSpec has no defaultable fields, so nothing is omitted here.


_GRID_LENGTHS = (
    "margin_left",
    "margin_top",
    "margin_right",
    "margin_bottom",
    "gap_x",
    "gap_y",
    "label_width",
    "label_height",
)


class GridSpec(_Base):
    rows: Annotated[int, Field(ge=1, le=1_000)]
    cols: Annotated[int, Field(ge=1, le=1_000)]
    margin_left_mm: Length = 0.0
    margin_top_mm: Length = 0.0
    margin_right_mm: Length = 0.0
    margin_bottom_mm: Length = 0.0
    gap_x_mm: Length = 0.0
    gap_y_mm: Length = 0.0
    label_width_mm: OptionalLength = None
    label_height_mm: OptionalLength = None
    authored_unit: Unit = Field(default="mm", alias="_authored_unit", exclude=True)

    model_config = ConfigDict(**{**_BASE_CONFIG, "populate_by_name": True})

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        return _resolve_lengths(data, _GRID_LENGTHS, "grid")

    @field_validator("rows", "cols", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, bool):
            raise ValueError(f"grid.{info.field_name} must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _check_cells(self) -> GridSpec:
        if self.rows * self.cols > MAX_GRID_CELLS:
            raise ValueError(
                f"grid has {self.rows * self.cols} cells; the maximum is {MAX_GRID_CELLS}"
            )
        return self

    @property
    def cells_per_page(self) -> int:
        return self.rows * self.cols

    def dump(self, unit: Unit) -> dict[str, Any]:
        """Always emit rows/cols and the four margins; omit zero gaps.

        The margins are load-bearing for alignment on real label stock, so
        stating them explicitly is worth the extra lines even when zero.
        """
        payload = self.model_dump(mode="json", exclude_none=True)
        keep_zero = {
            "rows",
            "cols",
            "margin_left_mm",
            "margin_top_mm",
            "margin_right_mm",
            "margin_bottom_mm",
            "label_width_mm",
            "label_height_mm",
        }
        payload = {
            key: value
            for key, value in payload.items()
            if key in keep_zero or value not in (0, 0.0)
        }
        return _dump_lengths(payload, _GRID_LENGTHS, unit)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


class LabelTemplate(_Base):
    """A full sheet definition: page size, grid, and the elements per label."""

    template_type: Literal["label"] = "label"
    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    page: PageSpec
    grid: GridSpec
    elements: list[Element] = Field(default_factory=list, max_length=MAX_ELEMENTS)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def units(self) -> Unit:
        """The unit this document was authored in."""
        if "in" in (self.page.authored_unit, self.grid.authored_unit):
            return "in"
        return "mm"

    @property
    def field_names(self) -> list[str]:
        return _ordered_field_names(self.elements)

    def dump(self) -> dict[str, Any]:
        unit = self.units
        payload: dict[str, Any] = {"template_type": "label"}
        if self.name:
            payload["name"] = self.name
        payload["page"] = self.page.dump(unit)
        payload["grid"] = self.grid.dump(unit)
        payload["elements"] = [element.dump(unit) for element in self.elements]
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


class TextLayoutTemplate(_Base):
    """Elements only. Reuses another template's page and grid geometry."""

    template_type: Literal["text-layout"] = "text-layout"
    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    elements: list[Element] = Field(default_factory=list, max_length=MAX_ELEMENTS)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def units(self) -> Unit:
        return "in" if any(e.authored_unit == "in" for e in self.elements) else "mm"

    @property
    def field_names(self) -> list[str]:
        return _ordered_field_names(self.elements)

    def dump(self) -> dict[str, Any]:
        unit = self.units
        payload: dict[str, Any] = {"template_type": "text-layout"}
        if self.name:
            payload["name"] = self.name
        payload["elements"] = [element.dump(unit) for element in self.elements]
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


# Union rather than `|`: this alias is evaluated by pydantic at runtime, and on
# Python 3.10 the PEP 604 form is not subscriptable in every context it reaches.
TemplateDefinition = Union[LabelTemplate, TextLayoutTemplate]  # noqa: UP007


def _ordered_field_names(elements: list[Any]) -> list[str]:
    """Field names in element order, with ``name`` hoisted first if present."""
    seen: list[str] = []
    for element in elements:
        for field_name in element.field_names:
            if field_name not in seen:
                seen.append(field_name)
    if "name" in seen:
        return ["name", *(f for f in seen if f != "name")]
    return seen
