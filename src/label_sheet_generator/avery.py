"""Built-in label stock presets, declared in exact published inches.

Every preset is authored in the dimensions the manufacturer publishes -- inches
for the US letter stock, millimetres converted to inches for the A4 stock --
and the millimetre values are *derived* through :mod:`label_sheet_generator.units`.
The previous table stored pre-rounded millimetre literals, which made two things
impossible: checking a preset against its datasheet without reversing the
conversion, and keeping full precision. Avery 5160's 2-5/8in label is exactly
66.675mm; the old ``66.67`` literal drifted the third column by a visible
fraction of a millimetre across a sheet.

Each preset self-checks at import time: on both axes the margins, labels and
gaps must add up to the page. A mistyped digit therefore fails loudly when the
module loads rather than silently producing a sheet that prints off the edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from label_sheet_generator.errors import ConfigurationError, NotFoundError
from label_sheet_generator.schema import GridSpec, LabelTemplate, PageSpec
from label_sheet_generator.units import in_to_mm, mm_to_in, quantize

#: The sheet sizes presets may be printed on.
Stock = Literal["letter", "a4"]

__all__ = [
    "AveryPreset",
    "Stock",
    "build_template",
    "get_preset",
    "iter_presets",
    "normalize_code",
]

#: Tolerance for the per-axis self-consistency check. 0.01in is 0.254mm: below
#: the printing accuracy of any consumer sheet-fed printer, but far above the
#: float noise of an inch-to-millimetre round trip, so it catches typos without
#: flagging honest rounding in a published figure.
_AXIS_TOLERANCE_IN = 0.01

#: US letter, the stock every Avery 5xxx preset below is printed on.
_LETTER_WIDTH_IN = 8.5
_LETTER_HEIGHT_IN = 11.0

#: ISO A4, published in millimetres and converted once here.
_A4_WIDTH_IN = mm_to_in(210.0)
_A4_HEIGHT_IN = mm_to_in(297.0)

#: Repeating inch fractions written as exact divisions rather than decimals.
#: "1-1/3 in" typed as 1.33 loses 0.023in over seven rows, which is a third of
#: a millimetre of creep by the bottom of the sheet.
_ONE_AND_ONE_THIRD_IN = 4.0 / 3.0
_THREE_AND_ONE_THIRD_IN = 10.0 / 3.0


@dataclass(frozen=True, slots=True)
class AveryPreset:
    """One sheet of label stock, in the units its datasheet publishes.

    All ``*_in`` fields are the authored values. The ``*_mm`` properties are
    derived, never stored, so the two can never disagree.
    """

    code: str
    aliases: tuple[str, ...]
    description: str
    rows: int
    cols: int
    label_width_in: float
    label_height_in: float
    margin_left_in: float
    margin_top_in: float
    margin_right_in: float
    margin_bottom_in: float
    gap_x_in: float
    gap_y_in: float
    page_width_in: float
    page_height_in: float
    stock: Stock
    source: str

    def __post_init__(self) -> None:
        """Verify both axes close to the page size.

        Raises:
            ConfigurationError: if a preset's own numbers do not add up. This
                is a defect in this file, not in user input, so it surfaces at
                import time.
        """
        self._check_axis(
            axis="width",
            span=(
                self.margin_left_in
                + self.cols * self.label_width_in
                + (self.cols - 1) * self.gap_x_in
                + self.margin_right_in
            ),
            page=self.page_width_in,
        )
        self._check_axis(
            axis="height",
            span=(
                self.margin_top_in
                + self.rows * self.label_height_in
                + (self.rows - 1) * self.gap_y_in
                + self.margin_bottom_in
            ),
            page=self.page_height_in,
        )

    def _check_axis(self, *, axis: str, span: float, page: float) -> None:
        discrepancy = span - page
        if abs(discrepancy) > _AXIS_TOLERANCE_IN:
            raise ConfigurationError(
                f"preset {self.code} does not close on the {axis} axis: margins, "
                f"labels and gaps span {span:.5f}in across a {page:.5f}in page, "
                f"a discrepancy of {discrepancy:+.5f}in "
                f"(tolerance {_AXIS_TOLERANCE_IN}in)"
            )

    @property
    def codes(self) -> tuple[str, ...]:
        """The canonical code followed by every accepted alias."""
        return (self.code, *self.aliases)

    @property
    def labels_per_sheet(self) -> int:
        """How many labels one sheet of this stock holds."""
        return self.rows * self.cols

    @property
    def label_width_mm(self) -> float:
        """Label width in millimetres."""
        return quantize(in_to_mm(self.label_width_in))

    @property
    def label_height_mm(self) -> float:
        """Label height in millimetres."""
        return quantize(in_to_mm(self.label_height_in))

    @property
    def margin_left_mm(self) -> float:
        """Left page margin in millimetres."""
        return quantize(in_to_mm(self.margin_left_in))

    @property
    def margin_top_mm(self) -> float:
        """Top page margin in millimetres."""
        return quantize(in_to_mm(self.margin_top_in))

    @property
    def margin_right_mm(self) -> float:
        """Right page margin in millimetres."""
        return quantize(in_to_mm(self.margin_right_in))

    @property
    def margin_bottom_mm(self) -> float:
        """Bottom page margin in millimetres."""
        return quantize(in_to_mm(self.margin_bottom_in))

    @property
    def gap_x_mm(self) -> float:
        """Horizontal gap between columns, in millimetres."""
        return quantize(in_to_mm(self.gap_x_in))

    @property
    def gap_y_mm(self) -> float:
        """Vertical gap between rows, in millimetres."""
        return quantize(in_to_mm(self.gap_y_in))

    @property
    def page_width_mm(self) -> float:
        """Page width in millimetres."""
        return quantize(in_to_mm(self.page_width_in))

    @property
    def page_height_mm(self) -> float:
        """Page height in millimetres."""
        return quantize(in_to_mm(self.page_height_in))


_PRESETS: tuple[AveryPreset, ...] = (
    AveryPreset(
        code="5160",
        aliases=("5260", "5960", "8160"),
        description="Address Labels, 1 x 2-5/8 in, 30 per sheet",
        rows=10,
        cols=3,
        label_width_in=2.625,
        label_height_in=1.0,
        margin_left_in=0.1875,
        margin_top_in=0.5,
        margin_right_in=0.1875,
        margin_bottom_in=0.5,
        gap_x_in=0.125,
        gap_y_in=0.0,
        page_width_in=_LETTER_WIDTH_IN,
        page_height_in=_LETTER_HEIGHT_IN,
        stock="letter",
        source=(
            "Avery 5160 datasheet: 8.5 x 11 in sheet, 0.1875 in side margins, "
            "0.5 in top/bottom margins, 2.75 in horizontal pitch, 1 in vertical pitch"
        ),
    ),
    AveryPreset(
        code="5161",
        aliases=("5261", "8161"),
        description="Address Labels, 1 x 4 in, 20 per sheet",
        rows=10,
        cols=2,
        label_width_in=4.0,
        label_height_in=1.0,
        margin_left_in=0.15625,
        margin_top_in=0.5,
        margin_right_in=0.15625,
        margin_bottom_in=0.5,
        gap_x_in=0.1875,
        gap_y_in=0.0,
        page_width_in=_LETTER_WIDTH_IN,
        page_height_in=_LETTER_HEIGHT_IN,
        stock="letter",
        source=(
            "Avery 5161 datasheet: 8.5 x 11 in sheet, 0.15625 in side margins, "
            "0.5 in top/bottom margins, 4.1875 in horizontal pitch, 1 in vertical pitch"
        ),
    ),
    AveryPreset(
        code="5162",
        aliases=("5262", "8162"),
        description="Address Labels, 1-1/3 x 4 in, 14 per sheet",
        rows=7,
        cols=2,
        label_width_in=4.0,
        label_height_in=_ONE_AND_ONE_THIRD_IN,
        margin_left_in=0.15625,
        # Seven 1-1/3in rows leave 1-2/3in of vertical slack; Avery publishes a
        # 0.83in top margin, and the remainder is split evenly top and bottom.
        margin_top_in=(_LETTER_HEIGHT_IN - 7.0 * _ONE_AND_ONE_THIRD_IN) / 2.0,
        margin_right_in=0.15625,
        margin_bottom_in=(_LETTER_HEIGHT_IN - 7.0 * _ONE_AND_ONE_THIRD_IN) / 2.0,
        gap_x_in=0.1875,
        gap_y_in=0.0,
        page_width_in=_LETTER_WIDTH_IN,
        page_height_in=_LETTER_HEIGHT_IN,
        stock="letter",
        source=(
            "Avery 5162 datasheet: 8.5 x 11 in sheet, 0.15625 in side margins, "
            "4.1875 in horizontal pitch, 1-1/3 in vertical pitch; the top/bottom "
            "margin split is derived so the seven rows close on the sheet height"
        ),
    ),
    AveryPreset(
        code="5163",
        aliases=("5263", "5963", "8163"),
        description="Shipping Labels, 2 x 4 in, 10 per sheet",
        rows=5,
        cols=2,
        label_width_in=4.0,
        label_height_in=2.0,
        margin_left_in=0.15625,
        margin_top_in=0.5,
        margin_right_in=0.15625,
        margin_bottom_in=0.5,
        gap_x_in=0.1875,
        gap_y_in=0.0,
        page_width_in=_LETTER_WIDTH_IN,
        page_height_in=_LETTER_HEIGHT_IN,
        stock="letter",
        source=(
            "Avery 5163 datasheet: 8.5 x 11 in sheet, 0.15625 in side margins, "
            "0.5 in top/bottom margins, 4.1875 in horizontal pitch, 2 in vertical pitch"
        ),
    ),
    AveryPreset(
        code="5164",
        aliases=("5264", "8164"),
        description="Shipping Labels, 3-1/3 x 4 in, 6 per sheet",
        rows=3,
        cols=2,
        label_width_in=4.0,
        label_height_in=_THREE_AND_ONE_THIRD_IN,
        margin_left_in=0.15625,
        margin_top_in=0.5,
        margin_right_in=0.15625,
        margin_bottom_in=0.5,
        gap_x_in=0.1875,
        gap_y_in=0.0,
        page_width_in=_LETTER_WIDTH_IN,
        page_height_in=_LETTER_HEIGHT_IN,
        stock="letter",
        source=(
            "Avery 5164 datasheet: 8.5 x 11 in sheet, 0.15625 in side margins, "
            "0.5 in top/bottom margins, 4.1875 in horizontal pitch, "
            "3-1/3 in vertical pitch"
        ),
    ),
    AveryPreset(
        code="L7160",
        aliases=("J8160",),
        description="Address Labels, 63.5 x 38.1 mm, 21 per A4 sheet",
        rows=7,
        cols=3,
        label_width_in=mm_to_in(63.5),
        label_height_in=mm_to_in(38.1),
        margin_left_in=mm_to_in(7.25),
        margin_top_in=mm_to_in(15.15),
        margin_right_in=mm_to_in(7.25),
        margin_bottom_in=mm_to_in(15.15),
        gap_x_in=mm_to_in(2.5),
        gap_y_in=0.0,
        page_width_in=_A4_WIDTH_IN,
        page_height_in=_A4_HEIGHT_IN,
        stock="a4",
        source=(
            "Avery L7160 datasheet: 210 x 297 mm sheet, 63.5 x 38.1 mm labels, "
            "66.0 mm horizontal pitch, 38.1 mm vertical pitch; Avery publishes "
            "asymmetric 7.21/7.29 mm side and 15.1/15.2 mm end margins, so the "
            "split is derived symmetrically (7.25 mm and 15.15 mm) to close the page"
        ),
    ),
    AveryPreset(
        code="L7163",
        aliases=("J8163",),
        description="Address Labels, 99.1 x 38.1 mm, 14 per A4 sheet",
        rows=7,
        cols=2,
        label_width_in=mm_to_in(99.1),
        label_height_in=mm_to_in(38.1),
        margin_left_in=mm_to_in(4.65),
        margin_top_in=mm_to_in(15.15),
        margin_right_in=mm_to_in(4.65),
        margin_bottom_in=mm_to_in(15.15),
        gap_x_in=mm_to_in(2.5),
        gap_y_in=0.0,
        page_width_in=_A4_WIDTH_IN,
        page_height_in=_A4_HEIGHT_IN,
        stock="a4",
        source=(
            "Avery L7163 datasheet: 210 x 297 mm sheet, 99.1 x 38.1 mm labels, "
            "101.6 mm horizontal pitch, 38.1 mm vertical pitch; the 4.65 mm side "
            "and 15.15 mm end margins are derived from the published pitch so "
            "both axes close on the page"
        ),
    ),
)

_BY_CODE: dict[str, AveryPreset] = {code: preset for preset in _PRESETS for code in preset.codes}


def normalize_code(code: str) -> str:
    """Fold a user-typed preset code to its lookup form.

    ``"avery 5160"``, ``"5160."`` and ``"l7160"`` all normalise to the stored
    code, because catalogues, spreadsheets and humans all punctuate these
    differently.
    """
    return "".join(character for character in str(code).upper() if character.isalnum())


def iter_presets() -> tuple[AveryPreset, ...]:
    """Every known preset, in catalogue order."""
    return _PRESETS


def get_preset(code: str) -> AveryPreset:
    """Look up a preset by canonical code or alias.

    Raises:
        NotFoundError: if no preset uses that code. An unknown code is a
            missing resource, not a malformed template, so this is a 404 and
            not a 422.
    """
    normalized = normalize_code(code)
    preset = _BY_CODE.get(normalized)
    if preset is None:
        known = ", ".join(item.code for item in _PRESETS)
        raise NotFoundError(
            f"unknown label stock preset {normalized or str(code)!r}; known presets are: {known}"
        )
    return preset


def build_template(code: str, *, name: str | None = None) -> LabelTemplate:
    """Build an empty :class:`~label_sheet_generator.schema.LabelTemplate`.

    The page and grid are handed to pydantic through their ``*_in`` keys so the
    authored unit recorded on the document is inches: a preset exported to disk
    then reads in the same units as its datasheet.

    Raises:
        NotFoundError: if ``code`` names no preset.
        ConfigurationError: if the preset does not satisfy the schema, which
            would be a defect in this module rather than in the request.
    """
    preset = get_preset(code)

    try:
        page = PageSpec.model_validate(
            {"width_in": preset.page_width_in, "height_in": preset.page_height_in}
        )
        grid = GridSpec.model_validate(
            {
                "rows": preset.rows,
                "cols": preset.cols,
                "margin_left_in": preset.margin_left_in,
                "margin_top_in": preset.margin_top_in,
                "margin_right_in": preset.margin_right_in,
                "margin_bottom_in": preset.margin_bottom_in,
                "gap_x_in": preset.gap_x_in,
                "gap_y_in": preset.gap_y_in,
                "label_width_in": preset.label_width_in,
                "label_height_in": preset.label_height_in,
            }
        )
        return LabelTemplate(
            name=name or f"avery-{preset.code.lower()}",
            page=page,
            grid=grid,
            metadata={
                "preset_brand": "Avery",
                "preset_code": normalize_code(code),
                "preset_canonical_code": preset.code,
                "preset_aliases": list(preset.aliases),
                "preset_description": preset.description,
                "preset_stock": preset.stock,
                "preset_source": preset.source,
            },
        )
    except PydanticValidationError as exc:
        raise ConfigurationError(
            f"preset {preset.code} does not satisfy the template schema: {exc.error_count()} "
            f"problem(s), first at {exc.errors()[0].get('loc')}"
        ) from exc
