from __future__ import annotations

from dataclasses import dataclass

from label_sheet_generator.models import GridSpec, LabelTemplate, PageSpec, TemplateError


@dataclass(frozen=True, slots=True)
class AveryPreset:
    canonical_code: str
    description: str
    rows: int
    cols: int
    label_width_mm: float
    label_height_mm: float
    margin_left_mm: float
    margin_top_mm: float
    margin_right_mm: float
    margin_bottom_mm: float
    gap_x_mm: float = 0.0
    gap_y_mm: float = 0.0
    page_width_mm: float = 215.9
    page_height_mm: float = 279.4
    aliases: tuple[str, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.canonical_code, *self.aliases)


_PRESET_GROUPS: tuple[AveryPreset, ...] = (
    AveryPreset(
        canonical_code="5160",
        aliases=("5260", "5960", "8160"),
        description="Address Labels, 1 x 2-5/8 in, 30 per sheet",
        rows=10,
        cols=3,
        label_width_mm=66.675,
        label_height_mm=25.4,
        margin_left_mm=4.7625,
        margin_top_mm=12.7,
        margin_right_mm=4.7625,
        margin_bottom_mm=12.7,
        gap_x_mm=3.175,
        gap_y_mm=0.0,
    ),
    AveryPreset(
        canonical_code="5161",
        aliases=("5261", "8161"),
        description="Address Labels, 1 x 4 in, 20 per sheet",
        rows=10,
        cols=2,
        label_width_mm=101.6,
        label_height_mm=25.4,
        margin_left_mm=4.7625,
        margin_top_mm=12.7,
        margin_right_mm=4.7625,
        margin_bottom_mm=12.7,
        gap_x_mm=3.175,
        gap_y_mm=0.0,
    ),
    AveryPreset(
        canonical_code="5163",
        aliases=("5263", "5963", "8163"),
        description="Shipping Labels, 2 x 4 in, 10 per sheet",
        rows=5,
        cols=2,
        label_width_mm=101.6,
        label_height_mm=50.8,
        margin_left_mm=4.7625,
        margin_top_mm=12.7,
        margin_right_mm=4.7625,
        margin_bottom_mm=12.7,
        gap_x_mm=3.175,
        gap_y_mm=0.0,
    ),
    AveryPreset(
        canonical_code="5164",
        aliases=("5264", "8164"),
        description="Shipping Labels, 3-1/3 x 4 in, 6 per sheet",
        rows=3,
        cols=2,
        label_width_mm=101.6,
        label_height_mm=84.667,
        margin_left_mm=4.7625,
        margin_top_mm=12.7,
        margin_right_mm=4.7625,
        margin_bottom_mm=12.7,
        gap_x_mm=3.175,
        gap_y_mm=0.0,
    ),
)


def normalize_avery_code(code: str) -> str:
    return "".join(character for character in str(code).upper() if character.isalnum())


def iter_avery_preset_groups() -> tuple[AveryPreset, ...]:
    return _PRESET_GROUPS


def get_avery_preset(code: str) -> AveryPreset:
    normalized_code = normalize_avery_code(code)
    for preset in _PRESET_GROUPS:
        if normalized_code in preset.codes:
            return preset
    raise TemplateError(f"unsupported Avery preset code: {code}")


def build_template_from_avery_preset(code: str, *, name: str | None = None) -> LabelTemplate:
    normalized_code = normalize_avery_code(code)
    preset = get_avery_preset(normalized_code)

    return LabelTemplate(
        name=name or f"avery-{normalized_code.lower()}",
        page=PageSpec(width_mm=preset.page_width_mm, height_mm=preset.page_height_mm),
        grid=GridSpec(
            rows=preset.rows,
            cols=preset.cols,
            margin_left_mm=preset.margin_left_mm,
            margin_top_mm=preset.margin_top_mm,
            margin_right_mm=preset.margin_right_mm,
            margin_bottom_mm=preset.margin_bottom_mm,
            gap_x_mm=preset.gap_x_mm,
            gap_y_mm=preset.gap_y_mm,
            label_width_mm=preset.label_width_mm,
            label_height_mm=preset.label_height_mm,
        ),
        metadata={
            "preset_brand": "Avery",
            "preset_code": normalized_code,
            "preset_canonical_code": preset.canonical_code,
            "preset_description": preset.description,
        },
    )
