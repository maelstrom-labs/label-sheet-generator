from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from label_sheet_generator.units import in_to_mm


class TemplateError(ValueError):
    pass


ElementType = Literal["text", "barcode", "image"]
Alignment = Literal["left", "center", "right", "justify"]
BarcodeType = Literal["code128", "ean13", "qr"]
_MISSING = object()
_ALIGNMENTS = {"left", "center", "right", "justify"}


@dataclass(slots=True)
class PageSpec:
    width_mm: float
    height_mm: float

    def to_dict(self) -> dict[str, float]:
        return {
            "width_mm": float(self.width_mm),
            "height_mm": float(self.height_mm),
        }


@dataclass(slots=True)
class GridSpec:
    rows: int
    cols: int
    margin_left_mm: float
    margin_top_mm: float
    margin_right_mm: float
    margin_bottom_mm: float
    gap_x_mm: float = 0.0
    gap_y_mm: float = 0.0
    label_width_mm: float | None = None
    label_height_mm: float | None = None

    def resolved_label_width_mm(self, page: PageSpec) -> float:
        if self.label_width_mm is not None:
            return self.label_width_mm
        usable_width = (
            page.width_mm
            - self.margin_left_mm
            - self.margin_right_mm
            - self.gap_x_mm * max(self.cols - 1, 0)
        )
        width = usable_width / self.cols
        if width <= 0:
            raise TemplateError("grid layout produced a non-positive label width")
        return width

    def resolved_label_height_mm(self, page: PageSpec) -> float:
        if self.label_height_mm is not None:
            return self.label_height_mm
        usable_height = (
            page.height_mm
            - self.margin_top_mm
            - self.margin_bottom_mm
            - self.gap_y_mm * max(self.rows - 1, 0)
        )
        height = usable_height / self.rows
        if height <= 0:
            raise TemplateError("grid layout produced a non-positive label height")
        return height

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "margin_left_mm": float(self.margin_left_mm),
            "margin_top_mm": float(self.margin_top_mm),
            "margin_right_mm": float(self.margin_right_mm),
            "margin_bottom_mm": float(self.margin_bottom_mm),
            "gap_x_mm": float(self.gap_x_mm),
            "gap_y_mm": float(self.gap_y_mm),
            "label_width_mm": None if self.label_width_mm is None else float(self.label_width_mm),
            "label_height_mm": None if self.label_height_mm is None else float(self.label_height_mm),
        }


@dataclass(slots=True)
class BaseElement:
    type: ElementType = field(init=False)
    x_mm: float
    y_mm: float
    width_mm: float | None = None
    height_mm: float | None = None
    field: str | None = None
    value: Any = None
    template: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "type": self.type,
            "x_mm": float(self.x_mm),
            "y_mm": float(self.y_mm),
        }
        if self.width_mm is not None:
            result["width_mm"] = float(self.width_mm)
        if self.height_mm is not None:
            result["height_mm"] = float(self.height_mm)
        if self.field is not None:
            result["field"] = self.field
        if self.value is not None:
            result["value"] = self.value
        if self.template is not None:
            result["template"] = self.template
        return result


@dataclass(slots=True)
class TextElement(BaseElement):
    type: Literal["text"] = field(init=False, default="text")
    font_name: str = "Helvetica"
    font_size_pt: float = 10.0
    leading_pt: float | None = None
    color: str = "#000000"
    align: Alignment = "left"
    rotation_deg: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "font_name": self.font_name,
                "font_size_pt": float(self.font_size_pt),
                "color": self.color,
                "align": self.align,
            }
        )
        if self.leading_pt is not None:
            result["leading_pt"] = float(self.leading_pt)
        if self.rotation_deg != 0.0:
            result["rotation_deg"] = float(self.rotation_deg)
        return result


@dataclass(slots=True)
class BarcodeElement(BaseElement):
    type: Literal["barcode"] = field(init=False, default="barcode")
    barcode_type: BarcodeType = "code128"
    human_readable: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "barcode_type": self.barcode_type,
                "human_readable": self.human_readable,
            }
        )
        return result


@dataclass(slots=True)
class ImageElement(BaseElement):
    type: Literal["image"] = field(init=False, default="image")
    preserve_aspect: bool = True

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["preserve_aspect"] = self.preserve_aspect
        return result


Element = TextElement | BarcodeElement | ImageElement


@dataclass(slots=True)
class LabelTemplate:
    page: PageSpec
    grid: GridSpec
    elements: list[Element] = field(default_factory=list)
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "page": self.page.to_dict(),
            "grid": self.grid.to_dict(),
            "elements": [element.to_dict() for element in self.elements],
        }
        if self.name:
            result["name"] = self.name
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass(slots=True)
class TextLayoutTemplate:
    elements: list[Element] = field(default_factory=list)
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "template_type": "text-layout",
            "elements": [element.to_dict() for element in self.elements],
        }
        if self.name:
            result["name"] = self.name
        if self.metadata:
            result["metadata"] = self.metadata
        return result


TemplateDefinition = LabelTemplate | TextLayoutTemplate


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"{field_name} must be a number") from exc


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"{field_name} must be an integer") from exc


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _coerce_float(value, field_name)


def _coerce_alignment(value: Any, field_name: str) -> Alignment:
    if not isinstance(value, str):
        raise TemplateError(f"{field_name} must be one of: left, center, right, justify")

    normalized_value = value.strip().lower()
    if normalized_value not in _ALIGNMENTS:
        raise TemplateError(f"{field_name} must be one of: left, center, right, justify")

    return normalized_value


def _coerce_length(
    raw_data: dict[str, Any],
    *,
    base_name: str,
    section_name: str,
    default: Any = _MISSING,
) -> float | None:
    millimeter_key = f"{base_name}_mm"
    inch_key = f"{base_name}_in"
    has_millimeters = millimeter_key in raw_data and raw_data[millimeter_key] is not None
    has_inches = inch_key in raw_data and raw_data[inch_key] is not None

    if has_millimeters and has_inches:
        raise TemplateError(
            f"{section_name} accepts either {millimeter_key} or {inch_key}, not both"
        )
    if has_millimeters:
        return _coerce_float(raw_data[millimeter_key], f"{section_name}.{millimeter_key}")
    if has_inches:
        return in_to_mm(_coerce_float(raw_data[inch_key], f"{section_name}.{inch_key}"))
    if default is _MISSING:
        raise TemplateError(f"{section_name} requires {millimeter_key} or {inch_key}")
    return default


def _parse_page(raw_page: dict[str, Any]) -> PageSpec:
    return PageSpec(
        width_mm=_coerce_length(raw_page, base_name="width", section_name="page.width"),
        height_mm=_coerce_length(raw_page, base_name="height", section_name="page.height"),
    )


def _parse_grid(raw_grid: dict[str, Any]) -> GridSpec:
    return GridSpec(
        rows=_coerce_int(raw_grid["rows"], "grid.rows"),
        cols=_coerce_int(raw_grid["cols"], "grid.cols"),
        margin_left_mm=_coerce_length(raw_grid, base_name="margin_left", section_name="grid.margin_left", default=0.0),
        margin_top_mm=_coerce_length(raw_grid, base_name="margin_top", section_name="grid.margin_top", default=0.0),
        margin_right_mm=_coerce_length(raw_grid, base_name="margin_right", section_name="grid.margin_right", default=0.0),
        margin_bottom_mm=_coerce_length(raw_grid, base_name="margin_bottom", section_name="grid.margin_bottom", default=0.0),
        gap_x_mm=_coerce_length(raw_grid, base_name="gap_x", section_name="grid.gap_x", default=0.0),
        gap_y_mm=_coerce_length(raw_grid, base_name="gap_y", section_name="grid.gap_y", default=0.0),
        label_width_mm=_coerce_length(raw_grid, base_name="label_width", section_name="grid.label_width", default=None),
        label_height_mm=_coerce_length(raw_grid, base_name="label_height", section_name="grid.label_height", default=None),
    )


def _parse_element(raw_element: dict[str, Any]) -> Element:
    element_type = raw_element.get("type")
    if element_type not in {"text", "barcode", "image"}:
        raise TemplateError(f"unsupported element type: {element_type!r}")

    common_kwargs = {
        "x_mm": _coerce_length(raw_element, base_name="x", section_name="element.x"),
        "y_mm": _coerce_length(raw_element, base_name="y", section_name="element.y"),
        "width_mm": _coerce_length(raw_element, base_name="width", section_name="element.width", default=None),
        "height_mm": _coerce_length(raw_element, base_name="height", section_name="element.height", default=None),
        "field": raw_element.get("field"),
        "value": raw_element.get("value"),
        "template": raw_element.get("template"),
    }

    if element_type == "text":
        return TextElement(
            **common_kwargs,
            font_name=raw_element.get("font_name", "Helvetica"),
            font_size_pt=_coerce_float(raw_element.get("font_size_pt", 10.0), "element.font_size_pt"),
            leading_pt=_optional_float(raw_element.get("leading_pt"), "element.leading_pt"),
            color=raw_element.get("color", "#000000"),
            align=_coerce_alignment(raw_element.get("align", "left"), "element.align"),
            rotation_deg=_coerce_float(raw_element.get("rotation_deg", 0.0), "element.rotation_deg"),
        )

    if element_type == "barcode":
        if common_kwargs["width_mm"] is None or common_kwargs["height_mm"] is None:
            raise TemplateError("barcode elements require width_mm and height_mm")
        return BarcodeElement(
            **common_kwargs,
            barcode_type=raw_element.get("barcode_type", "code128"),
            human_readable=bool(raw_element.get("human_readable", False)),
        )

    if common_kwargs["width_mm"] is None or common_kwargs["height_mm"] is None:
        raise TemplateError("image elements require width_mm and height_mm")
    return ImageElement(
        **common_kwargs,
        preserve_aspect=bool(raw_element.get("preserve_aspect", True)),
    )


def parse_template(data: dict[str, Any]) -> LabelTemplate:
    if "page" not in data:
        raise TemplateError("template is missing the 'page' section")
    if "grid" not in data:
        raise TemplateError("template is missing the 'grid' section")

    page = _parse_page(data["page"])
    grid = _parse_grid(data["grid"])
    elements = [_parse_element(raw_element) for raw_element in data.get("elements", [])]

    return LabelTemplate(
        page=page,
        grid=grid,
        elements=elements,
        name=data.get("name"),
        metadata=dict(data.get("metadata") or {}),
    )


def parse_text_layout_template(data: dict[str, Any]) -> TextLayoutTemplate:
    if "page" in data or "grid" in data:
        raise TemplateError("text layout templates cannot define 'page' or 'grid'")

    elements = [_parse_element(raw_element) for raw_element in data.get("elements", [])]
    return TextLayoutTemplate(
        elements=elements,
        name=data.get("name"),
        metadata=dict(data.get("metadata") or {}),
    )


def parse_template_definition(data: dict[str, Any]) -> TemplateDefinition:
    template_type = data.get("template_type")

    if template_type == "text-layout":
        return parse_text_layout_template(data)
    if template_type not in {None, "label"}:
        raise TemplateError(f"unsupported template_type: {template_type!r}")

    if "page" in data or "grid" in data:
        return parse_template(data)
    if "elements" in data:
        return parse_text_layout_template(data)
    return parse_template(data)
