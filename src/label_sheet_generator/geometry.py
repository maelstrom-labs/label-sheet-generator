"""Pure geometry: where each label sits, and whether it actually fits.

No filesystem, no PDF library, no I/O -- just arithmetic over a validated
template, which makes this the cheapest thing to test and the thing the API
calls to answer "will this work?" without rendering.

The previous implementation got validation wrong in both directions at once:
it rejected valid grids (comparing a right edge against page width with a bare
``0.01`` fudge, so a grid that fit exactly could fail) and accepted invalid
ones (``margin_right_mm`` and ``margin_bottom_mm`` were parsed, stored, and
then never consulted, so a grid could silently overrun its own declared
margins). Both are fixed here: one named tolerance, applied symmetrically, and
all four margins are enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from label_sheet_generator.errors import GeometryError
from label_sheet_generator.schema import LabelTemplate
from label_sheet_generator.units import mm_to_pt

#: Lengths within this many millimetres of each other are treated as equal.
#: 0.01mm is 10 microns -- far below what any printer resolves, and below the
#: rounding error of a template authored to 2 decimal places.
TOLERANCE_MM = 0.01


@dataclass(frozen=True, slots=True)
class Slot:
    """One label position, in PostScript points, origin at the page bottom-left."""

    index: int
    row: int
    col: int
    x_pt: float
    y_pt: float
    width_pt: float
    height_pt: float


@dataclass(frozen=True, slots=True)
class Issue:
    """A geometry problem. ``code`` is stable; ``message`` is for humans."""

    code: str
    message: str
    axis: str | None = None
    overflow_mm: float | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.axis is not None:
            payload["axis"] = self.axis
        if self.overflow_mm is not None:
            payload["overflow_mm"] = round(self.overflow_mm, 4)
        return payload


@dataclass(frozen=True, slots=True)
class GeometryReport:
    """Everything known about whether a template's geometry is sound."""

    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    label_width_mm: float = 0.0
    label_height_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "label_width_mm": round(self.label_width_mm, 4),
            "label_height_mm": round(self.label_height_mm, 4),
        }


def resolve_label_size_mm(template: LabelTemplate) -> tuple[float, float]:
    """Return the label size, deriving it from the page when not declared.

    Raises:
        GeometryError: if the derived size is zero or negative, which means the
            margins and gaps already consume the whole page.
    """
    page, grid = template.page, template.grid

    if grid.label_width_mm is not None:
        width = grid.label_width_mm
    else:
        usable = (
            page.width_mm
            - grid.margin_left_mm
            - grid.margin_right_mm
            - grid.gap_x_mm * (grid.cols - 1)
        )
        width = usable / grid.cols
        if width <= TOLERANCE_MM:
            raise GeometryError(
                f"horizontal margins and gaps leave {usable:.2f}mm for {grid.cols} "
                "column(s); reduce the margins, the gap, or the column count",
                loc=("grid",),
            )

    if grid.label_height_mm is not None:
        height = grid.label_height_mm
    else:
        usable = (
            page.height_mm
            - grid.margin_top_mm
            - grid.margin_bottom_mm
            - grid.gap_y_mm * (grid.rows - 1)
        )
        height = usable / grid.rows
        if height <= TOLERANCE_MM:
            raise GeometryError(
                f"vertical margins and gaps leave {usable:.2f}mm for {grid.rows} "
                "row(s); reduce the margins, the gap, or the row count",
                loc=("grid",),
            )

    return width, height


def validate(template: LabelTemplate) -> GeometryReport:
    """Check a template's geometry without rendering it.

    Errors mean the sheet cannot be produced. Warnings mean it can, but
    something is probably not what the author intended -- an element hanging
    outside its label, or a grid that overruns a declared margin without
    running off the page.
    """
    errors: list[Issue] = []
    warnings: list[Issue] = []

    try:
        label_width_mm, label_height_mm = resolve_label_size_mm(template)
    except GeometryError as exc:
        return GeometryReport(errors=[Issue("grid_does_not_fit", exc.message)])

    page, grid = template.page, template.grid

    span_x = grid.margin_left_mm + grid.cols * label_width_mm + (grid.cols - 1) * grid.gap_x_mm
    span_y = grid.margin_top_mm + grid.rows * label_height_mm + (grid.rows - 1) * grid.gap_y_mm

    # Off the page is fatal: ReportLab would happily draw into nothing.
    if span_x > page.width_mm + TOLERANCE_MM:
        errors.append(
            Issue(
                "grid_exceeds_page",
                f"the grid is {span_x:.2f}mm wide but the page is only "
                f"{page.width_mm:.2f}mm; it overruns the right edge by "
                f"{span_x - page.width_mm:.2f}mm",
                axis="x",
                overflow_mm=span_x - page.width_mm,
            )
        )
    if span_y > page.height_mm + TOLERANCE_MM:
        errors.append(
            Issue(
                "grid_exceeds_page",
                f"the grid is {span_y:.2f}mm tall but the page is only "
                f"{page.height_mm:.2f}mm; it overruns the bottom edge by "
                f"{span_y - page.height_mm:.2f}mm",
                axis="y",
                overflow_mm=span_y - page.height_mm,
            )
        )

    # Inside the page but past a declared margin is a warning: the sheet prints,
    # but the author wrote a margin the layout does not honour.
    right_limit = page.width_mm - grid.margin_right_mm
    if not errors and span_x > right_limit + TOLERANCE_MM:
        warnings.append(
            Issue(
                "grid_exceeds_margin",
                f"the grid reaches {span_x:.2f}mm, past the declared right margin "
                f"at {right_limit:.2f}mm; margin_right_mm is not being honoured",
                axis="x",
                overflow_mm=span_x - right_limit,
            )
        )
    bottom_limit = page.height_mm - grid.margin_bottom_mm
    if not errors and span_y > bottom_limit + TOLERANCE_MM:
        warnings.append(
            Issue(
                "grid_exceeds_margin",
                f"the grid reaches {span_y:.2f}mm, past the declared bottom margin "
                f"at {bottom_limit:.2f}mm; margin_bottom_mm is not being honoured",
                axis="y",
                overflow_mm=span_y - bottom_limit,
            )
        )

    for index, element in enumerate(template.elements):
        width = element.width_mm if element.width_mm is not None else label_width_mm - element.x_mm
        height = (
            element.height_mm if element.height_mm is not None else label_height_mm - element.y_mm
        )

        if width <= TOLERANCE_MM or height <= TOLERANCE_MM:
            errors.append(
                Issue(
                    "element_has_no_area",
                    f"element {index} ({element.type}) resolves to "
                    f"{width:.2f} x {height:.2f}mm, which has no drawable area",
                )
            )
            continue

        if element.x_mm + width > label_width_mm + TOLERANCE_MM:
            warnings.append(
                Issue(
                    "element_exceeds_label",
                    f"element {index} ({element.type}) reaches "
                    f"{element.x_mm + width:.2f}mm across a {label_width_mm:.2f}mm "
                    "label and will be clipped",
                    axis="x",
                    overflow_mm=element.x_mm + width - label_width_mm,
                )
            )
        if element.y_mm + height > label_height_mm + TOLERANCE_MM:
            warnings.append(
                Issue(
                    "element_exceeds_label",
                    f"element {index} ({element.type}) reaches "
                    f"{element.y_mm + height:.2f}mm down a {label_height_mm:.2f}mm "
                    "label and will be clipped",
                    axis="y",
                    overflow_mm=element.y_mm + height - label_height_mm,
                )
            )

    return GeometryReport(
        errors=errors,
        warnings=warnings,
        label_width_mm=label_width_mm,
        label_height_mm=label_height_mm,
    )


def compute_slots(template: LabelTemplate) -> list[Slot]:
    """Return every label position on one page, in reading order.

    Raises:
        GeometryError: if the geometry has a fatal problem. Callers wanting the
            warnings as well should use :func:`validate` first.
    """
    report = validate(template)
    if report.errors:
        raise GeometryError(
            report.errors[0].message,
            loc=("grid",),
            details=[issue.to_dict() for issue in report.errors],
        )

    grid = template.grid
    page_height_pt = mm_to_pt(template.page.height_mm)
    label_width_pt = mm_to_pt(report.label_width_mm)
    label_height_pt = mm_to_pt(report.label_height_mm)
    margin_left_pt = mm_to_pt(grid.margin_left_mm)
    margin_top_pt = mm_to_pt(grid.margin_top_mm)
    gap_x_pt = mm_to_pt(grid.gap_x_mm)
    gap_y_pt = mm_to_pt(grid.gap_y_mm)

    slots: list[Slot] = []
    for row in range(grid.rows):
        top_pt = margin_top_pt + row * (label_height_pt + gap_y_pt)
        y_pt = page_height_pt - top_pt - label_height_pt
        for col in range(grid.cols):
            slots.append(
                Slot(
                    index=len(slots),
                    row=row,
                    col=col,
                    x_pt=margin_left_pt + col * (label_width_pt + gap_x_pt),
                    y_pt=y_pt,
                    width_pt=label_width_pt,
                    height_pt=label_height_pt,
                )
            )
    return slots
