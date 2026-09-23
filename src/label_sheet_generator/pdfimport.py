"""Derive a label template from an existing sheet PDF.

Given the bytes of a PDF that already shows a label grid -- a manufacturer's
alignment sheet, or a sheet this package produced with ``--borders`` -- this
module measures the grid and hands back a validated
:class:`~label_sheet_generator.schema.LabelTemplate`.

This is the only place in the package that parses a file format the caller
does not control, so every step is bounded and every failure is typed:

* **Bytes in, never a path.** An HTTP upload can be imported without touching
  the filesystem, and nothing here can be pointed at a file the caller did not
  hand over. The CLI owns file access; this module does not.
* **Named limits, not hope.** :data:`MAX_PDF_BYTES`, :data:`MAX_PDF_PAGES`,
  :data:`MAX_VECTOR_OBJECTS` and :data:`PARSE_TIMEOUT_S` each bound one way a
  hostile or merely enormous document can burn the process, and each violation
  names itself in a :class:`~label_sheet_generator.errors.LimitExceeded`.
* **A detection that cannot be right is discarded rather than returned.** The
  previous importer could emit a confidently wrong template -- gap and label
  size swapped, or measured in a rotated coordinate space -- which is worse
  than no detection at all, because the user only finds out after printing a
  sheet of stock. Every candidate is checked against the page before it is
  returned, and the reason a candidate was thrown away is reported as a
  warning rather than silently swallowed.
* **Confidence is measured, not asserted.** See :func:`_confidence`.

Two honest caveats.

:data:`PARSE_TIMEOUT_S` is enforced by running the parse on a daemon thread
and abandoning it at the deadline. CPython cannot interrupt a running thread,
so an abandoned parse keeps going until it finishes: the deadline bounds *the
caller's* wait, not the work. The hard bounds on work are
:data:`MAX_PDF_BYTES` and :data:`MAX_VECTOR_OBJECTS`. ``signal`` is not used
for this because it is main-thread only and would break under a web server.

:data:`MAX_VECTOR_OBJECTS` is checked once the page's object lists exist,
because their length is not knowable before the page is parsed. It therefore
bounds the clustering work and refuses pathological documents; it does not
bound the parse that produced the count.

``pdfplumber`` and ``pypdf`` are optional extras. Importing this module must
never require them, so both imports live inside the functions that use them
and a missing install surfaces as a
:class:`~label_sheet_generator.errors.ConfigurationError` naming the extra.
"""

from __future__ import annotations

import io
import math
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from itertools import pairwise
from statistics import fmean, median
from typing import Any, Literal, TypeVar

from pydantic import ValidationError as PydanticValidationError

from label_sheet_generator.avery import AveryPreset, get_preset, normalize_code
from label_sheet_generator.errors import (
    ConfigurationError,
    LabelSheetError,
    LimitExceeded,
    TemplateError,
    ValidationError,
)
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.schema import MAX_GRID_CELLS, MAX_NAME_LENGTH, LabelTemplate
from label_sheet_generator.units import pt_to_mm, quantize

#: Which page feature the grid was measured from. Ordered by how much it is
#: worth trusting: a rectangle states a box outright, a curve is a box only if
#: it happens to be drawn as one, and a set of rules is an inference.
DetectionMethod = Literal["rectangles", "curves", "lines"]

__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_VECTOR_OBJECTS",
    "PARSE_TIMEOUT_S",
    "DetectedGrid",
    "DetectionMethod",
    "ImportReport",
    "detect_grid",
    "import_template",
    "read_page_size_mm",
]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Largest PDF accepted for import. A label sheet is vector line-work: the
#: busiest real alignment sheet is a few hundred kilobytes, and a full-page
#: scan at 600dpi is under 10MB. 16MB is generous for both and still an
#: allocation a small container survives.
MAX_PDF_BYTES = 16 * 1024 * 1024

#: Only page 1 is measured, but a document is rejected outright past this many
#: pages rather than ignoring the rest: a 50,000-page file offered as a
#: template is not a label sheet, and building its page tree is the expensive
#: part. 64 leaves room for a multi-page sample pack.
MAX_PDF_PAGES = 64

#: Cap on rectangles + curves + lines examined on page 1. A label grid needs
#: one vector per cell plus a border or two -- a 10,000-cell sheet is already
#: the schema's hard maximum. 20,000 accepts every real sheet and refuses a
#: document whose million-object vector soup exists only to be chewed on.
MAX_VECTOR_OBJECTS = 20_000

#: Wall-clock budget for one parse. Reading a real label sheet takes tens of
#: milliseconds; anything still running after 15s is not going to produce a
#: useful answer within a request. See the module docstring for what this
#: bounds and what it does not.
PARSE_TIMEOUT_S = 15.0

#: Below this, :func:`import_template` warns that the result must be checked
#: against the physical sheet. Set at the score a line-derived detection can
#: never quite reach (see :func:`_confidence`), because boundary inference
#: cannot tell a label edge from a decorative rule, and a rectangle-derived
#: detection only falls here when the grid is visibly incomplete.
LOW_CONFIDENCE_THRESHOLD = 0.7


# ---------------------------------------------------------------------------
# Detection tuning
# ---------------------------------------------------------------------------

_PDF_HEADER = b"%PDF-"

#: How far into the file the header may sit. The spec puts it at byte 0, but
#: real generators prepend junk and every reader tolerates a short lead-in.
_HEADER_SCAN_BYTES = 1024

#: Two positions or sizes within this many points are the same one. 2pt is
#: 0.7mm: wider than the stroke width and coordinate rounding of any generator,
#: narrower than the smallest gap on real label stock.
_CLUSTER_TOLERANCE_PT = 2.0

#: A box smaller than this on either axis is decoration -- a tick mark, a
#: registration cross, a checkbox -- not a label. 20pt is 7mm.
_MIN_BOX_SIZE_PT = 20.0

#: A rule shorter than this is a tick, not a grid boundary.
_MIN_LINE_LENGTH_PT = 20.0

#: One box is an accident; a grid needs at least two of the same size.
_MIN_CANDIDATE_BOXES = 2

#: Boundary inference needs at least a left and a right edge.
_MIN_BOUNDARIES = 2

#: How many distinct size clusters are tried before giving up. Bounds the work
#: and stops a noisy page from being searched for a grid that is not there.
_MAX_SIZE_CLUSTERS = 8

#: A boundary rule is kept if it is at least this fraction as long as the
#: longest one found, which drops the short rules inside a label while keeping
#: the full-height column separators.
_PROMINENCE_RATIO = 0.5

#: In boundary inference, an interval shorter than this fraction of the widest
#: interval is a gap rather than a label.
_GAP_RATIO = 0.75

#: How far the measured grid may overrun the page before it is refused. 1mm is
#: larger than any accumulation of stroke-width and rounding error across a
#: sheet, and small enough that a genuinely wrong grid cannot hide inside it.
_SPAN_TOLERANCE_MM = 1.0

#: How far the two parsers' page sizes may differ before the detection is
#: discarded as measured in a different coordinate space. 0.5mm is well under
#: the smallest difference any rotation or crop produces.
_PAGE_MATCH_TOLERANCE_MM = 0.5

#: Anything smaller than this is not a label; it is measurement noise.
_MIN_LABEL_MM = 1.0

#: A size cluster covering less than this fraction of the cells it implies is
#: not a grid, it is a few boxes that happen to match.
_MIN_COVERAGE = 0.5

#: Evidence weights in :func:`_confidence`. Coverage dominates because a full
#: set of equally sized boxes is the strongest signal available; closure is
#: corroboration, since a sheet with a large trailing margin is legitimate.
_COVERAGE_WEIGHT = 0.6
_CLOSURE_WEIGHT = 0.4

#: How much each method's evidence is worth on its own. See
#: :data:`DetectionMethod`.
_METHOD_WEIGHT: dict[DetectionMethod, float] = {
    "rectangles": 1.0,
    "curves": 0.85,
    "lines": 0.7,
}

#: Right-angle rotations swap the page's width and height.
_QUARTER_TURN_DEG = 90
_THREE_QUARTER_TURN_DEG = 270
_FULL_TURN_DEG = 360

_CONFIDENCE_PRECISION = 3
_DEFAULT_TEMPLATE_NAME = "imported-template"
_PDF_SUFFIX = ".pdf"
_INSTALL_HINT = "install the import extra: pip install 'label-sheet-generator[pdfimport]'"

_R = TypeVar("_R")
_Number = TypeVar("_Number", int, float)


# ---------------------------------------------------------------------------
# Public results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectedGrid:
    """A label grid measured off a page, in millimetres.

    Every instance has already passed :func:`_rejection_reason`, so the values
    are internally consistent and fit the page they were measured on.
    """

    rows: int
    cols: int
    label_width_mm: float
    label_height_mm: float
    margin_left_mm: float
    margin_top_mm: float
    gap_x_mm: float
    gap_y_mm: float
    method: DetectionMethod
    #: 0.0-1.0. Observed evidence, not a guess; see :func:`_confidence`.
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe view of the detection."""
        return {
            "rows": self.rows,
            "cols": self.cols,
            "label_width_mm": self.label_width_mm,
            "label_height_mm": self.label_height_mm,
            "margin_left_mm": self.margin_left_mm,
            "margin_top_mm": self.margin_top_mm,
            "gap_x_mm": self.gap_x_mm,
            "gap_y_mm": self.gap_y_mm,
            "method": self.method,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ImportReport:
    """The outcome of an import: the template, and everything questionable.

    ``warnings`` is the honest part of the result. It carries the reasons a
    candidate detection was thrown away, a low-confidence caution, and any
    geometry warning the resulting template produced.
    """

    template: LabelTemplate
    detected: DetectedGrid | None
    page_size_mm: tuple[float, float]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe view of the whole report."""
        return {
            "template": self.template.dump(),
            "detected": None if self.detected is None else self.detected.to_dict(),
            "page_size_mm": [quantize(self.page_size_mm[0]), quantize(self.page_size_mm[1])],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Internal page model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Box:
    """An axis-aligned vector object in pdfplumber's top-left point space."""

    x0: float
    top: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class _PageVectors:
    """Page 1 reduced to plain numbers, so no library object outlives the parse."""

    width_pt: float
    height_pt: float
    rects: tuple[_Box, ...]
    curves: tuple[_Box, ...]
    lines: tuple[_Box, ...]


@dataclass(frozen=True, slots=True)
class _Analysis:
    """A detection attempt plus the page it was measured against."""

    grid: DetectedGrid | None
    page_width_mm: float
    page_height_mm: float
    warnings: list[str]


# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------


def _import_pdfplumber() -> Any:
    """Import pdfplumber, or explain how to install it."""
    try:
        import pdfplumber  # noqa: PLC0415 - optional extra, imported on use
    except ImportError as exc:
        raise ConfigurationError(
            f"PDF template import requires pdfplumber; {_INSTALL_HINT}"
        ) from exc
    return pdfplumber


def _import_pypdf() -> Any:
    """Import pypdf, or explain how to install it."""
    try:
        import pypdf  # noqa: PLC0415 - optional extra, imported on use
    except ImportError as exc:
        raise ConfigurationError(f"PDF template import requires pypdf; {_INSTALL_HINT}") from exc
    return pypdf


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _check_pdf_bytes(pdf_bytes: bytes) -> None:
    """Reject anything that is not a plausibly sized PDF before parsing it.

    Raises:
        ValidationError: if the input is not bytes, is empty, or is not a PDF.
        LimitExceeded: if it is larger than :data:`MAX_PDF_BYTES`.
    """
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise ValidationError("the PDF must be supplied as bytes")

    size = len(pdf_bytes)
    if size == 0:
        raise ValidationError("the PDF is empty")
    if size > MAX_PDF_BYTES:
        raise LimitExceeded(
            f"the PDF is {size} bytes; the maximum accepted for import is {MAX_PDF_BYTES}",
            limit_name="max_pdf_bytes",
            limit=MAX_PDF_BYTES,
            actual=size,
        )
    if _PDF_HEADER not in bytes(pdf_bytes[:_HEADER_SCAN_BYTES]):
        raise ValidationError(
            "the file does not carry a %PDF header in its first "
            f"{_HEADER_SCAN_BYTES} bytes, so it is not a PDF"
        )


def _check_page_count(page_count: int) -> None:
    """Reject an empty or implausibly long document.

    Raises:
        ValidationError: if there are no pages.
        LimitExceeded: if there are more than :data:`MAX_PDF_PAGES`.
    """
    if page_count < 1:
        raise ValidationError("the PDF has no pages")
    if page_count > MAX_PDF_PAGES:
        raise LimitExceeded(
            f"the PDF has {page_count} pages; import reads page 1 only and accepts "
            f"at most {MAX_PDF_PAGES}",
            limit_name="max_pdf_pages",
            limit=MAX_PDF_PAGES,
            actual=page_count,
        )


def _with_deadline(work: Callable[[], _R], *, what: str) -> _R:
    """Run ``work`` on a daemon thread, giving up after :data:`PARSE_TIMEOUT_S`.

    The thread is a daemon so an abandoned parse cannot keep the interpreter
    alive at exit. It cannot be killed -- see the module docstring.

    Raises:
        LimitExceeded: if the deadline passes with the work unfinished.
    """
    results: list[_R] = []
    failures: list[BaseException] = []

    def runner() -> None:
        try:
            results.append(work())
        except Exception as exc:  # re-raised below, on the calling thread
            failures.append(exc)

    thread = threading.Thread(target=runner, name="pdfimport-parse", daemon=True)
    thread.start()
    thread.join(PARSE_TIMEOUT_S)

    if thread.is_alive():
        raise LimitExceeded(
            f"{what} exceeded the {PARSE_TIMEOUT_S:g}s budget; the PDF is too complex to import",
            limit_name="parse_timeout_s",
            limit=PARSE_TIMEOUT_S,
        )
    if failures:
        raise failures[0]
    if not results:  # pragma: no cover - the thread either appends or raises
        raise ValidationError(f"{what} produced no result")
    return results[0]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def read_page_size_mm(pdf_bytes: bytes) -> tuple[float, float]:
    """Return page 1's size in millimetres, as it will be printed.

    The media box is the *unrotated* box, so a page carrying ``/Rotate 90``
    reports its width and height the wrong way round. They are swapped here so
    the answer matches what pdfplumber measures the grid against; a mismatch
    between the two is exactly how the old importer produced a transposed
    template.

    Raises:
        ValidationError: if the bytes are not a readable, unencrypted PDF.
        LimitExceeded: if a size, page-count or time limit is exceeded.
        ConfigurationError: if pypdf is not installed.
    """
    _check_pdf_bytes(pdf_bytes)
    return _with_deadline(lambda: _read_page_size(pdf_bytes), what="reading the PDF page size")


def _read_page_size(pdf_bytes: bytes) -> tuple[float, float]:
    """The pypdf half of :func:`read_page_size_mm`, run under the deadline."""
    pypdf = _import_pypdf()
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes), strict=False)
        if bool(reader.is_encrypted):
            raise ValidationError("the PDF is encrypted; decrypt it and import the decrypted copy")
        _check_page_count(len(reader.pages))

        page = reader.pages[0]
        width_pt = pt_to_mm(float(page.mediabox.width))
        height_pt = pt_to_mm(float(page.mediabox.height))
        rotation = int(getattr(page, "rotation", 0) or 0) % _FULL_TURN_DEG
    except LabelSheetError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"the PDF page size could not be read ({type(exc).__name__}); the file may be damaged"
        ) from exc

    if rotation in (_QUARTER_TURN_DEG, _THREE_QUARTER_TURN_DEG):
        width_pt, height_pt = height_pt, width_pt
    if width_pt <= 0.0 or height_pt <= 0.0:
        raise ValidationError("the PDF page has no printable area")
    return quantize(width_pt), quantize(height_pt)


def _extract_page_vectors(pdf_bytes: bytes) -> _PageVectors:
    """Reduce page 1 to plain numbers, under every applicable cap."""
    pdfplumber = _import_pdfplumber()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            _check_page_count(len(pdf.pages))
            page = pdf.pages[0]
            width_pt = float(page.width)
            height_pt = float(page.height)

            # The counts are only knowable once the page is parsed, so this
            # bounds the clustering below rather than the parse above.
            raw_rects = list(page.rects)
            raw_curves = list(page.curves)
            raw_lines = list(page.lines)
            total = len(raw_rects) + len(raw_curves) + len(raw_lines)
            if total > MAX_VECTOR_OBJECTS:
                raise LimitExceeded(
                    f"page 1 holds {total} vector objects; the maximum examined is "
                    f"{MAX_VECTOR_OBJECTS}",
                    limit_name="max_vector_objects",
                    limit=MAX_VECTOR_OBJECTS,
                    actual=total,
                )

            vectors = _PageVectors(
                width_pt=width_pt,
                height_pt=height_pt,
                rects=_to_boxes(raw_rects),
                curves=_to_boxes(raw_curves),
                lines=_to_boxes(raw_lines),
            )
    except LabelSheetError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"the PDF could not be parsed ({type(exc).__name__}); the file may be "
            "damaged or password protected"
        ) from exc

    if vectors.width_pt <= 0.0 or vectors.height_pt <= 0.0:
        raise ValidationError("the PDF page has no printable area")
    return vectors


def _to_boxes(objects: list[Any]) -> tuple[_Box, ...]:
    """Convert pdfplumber object dicts to :class:`_Box`, dropping unusable ones.

    pdfplumber yields ``Decimal`` as readily as ``float``, and a malformed
    content stream can yield a coordinate that is not a number at all. Anything
    that will not convert is dropped rather than raised on: one bad object is
    not a reason to refuse a page.
    """
    boxes: list[_Box] = []
    for obj in objects:
        try:
            x0 = float(obj["x0"])
            x1 = float(obj["x1"])
            top = float(obj["top"])
            bottom = float(obj["bottom"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if not all(map(_is_finite, (x0, x1, top, bottom))):
            continue
        boxes.append(
            _Box(
                x0=min(x0, x1),
                top=min(top, bottom),
                width=abs(x1 - x0),
                height=abs(bottom - top),
            )
        )
    return tuple(boxes)


def _is_finite(value: float) -> bool:
    """True if ``value`` is a real, finite number.

    NaN is the value that defeats every bounds check below by comparing False
    against all of them, so it is filtered out at the point of extraction.
    """
    return math.isfinite(value)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _cluster(values: Iterable[float], tolerance_pt: float = _CLUSTER_TOLERANCE_PT) -> list[float]:
    """Collapse near-equal values into their means, in ascending order."""
    buckets: list[list[float]] = []
    for value in sorted(values):
        if not buckets or abs(value - buckets[-1][-1]) > tolerance_pt:
            buckets.append([value])
        else:
            buckets[-1].append(value)
    return [fmean(bucket) for bucket in buckets]


def _cluster_weighted(
    values: list[tuple[float, float]],
    tolerance_pt: float = _CLUSTER_TOLERANCE_PT,
) -> list[tuple[float, float]]:
    """Cluster ``(position, weight)`` pairs, summing the weight per cluster."""
    positions: list[list[float]] = []
    weights: list[float] = []
    for position, weight in sorted(values, key=lambda item: item[0]):
        if not positions or abs(position - positions[-1][-1]) > tolerance_pt:
            positions.append([position])
            weights.append(weight)
        else:
            positions[-1].append(position)
            weights[-1] += weight
    return [(fmean(group), weight) for group, weight in zip(positions, weights, strict=True)]


def _cluster_sizes(values: list[float]) -> list[tuple[float, int]]:
    """Cluster interval lengths, returning ``(mean, member count)`` per cluster."""
    buckets: list[list[float]] = []
    for value in sorted(values):
        if not buckets or abs(value - buckets[-1][-1]) > _CLUSTER_TOLERANCE_PT:
            buckets.append([value])
        else:
            buckets[-1].append(value)
    return [(fmean(bucket), len(bucket)) for bucket in buckets]


def _median_gap(positions: list[float], size_pt: float) -> float:
    """The typical space between consecutive cells of ``size_pt``, never below 0."""
    if len(positions) <= 1:
        return 0.0
    deltas = [right - left - size_pt for left, right in pairwise(positions)]
    return max(median(deltas), 0.0)


# ---------------------------------------------------------------------------
# Confidence and sanity
# ---------------------------------------------------------------------------


def _axis_closure(span_mm: float, page_mm: float) -> float:
    """How tightly a measured span closes on the page, from 0.0 to 1.0.

    ``span`` already includes the leading margin, so whatever is left over is
    the trailing margin. A sheet that ends where the page ends scores 1.0; one
    that uses half the page scores 0.5.
    """
    if page_mm <= 0.0:
        return 0.0
    leftover = max(0.0, page_mm - span_mm)
    return max(0.0, 1.0 - leftover / page_mm)


def _confidence(
    *,
    method: DetectionMethod,
    matched: int,
    rows: int,
    cols: int,
    span_x_mm: float,
    span_y_mm: float,
    page_width_mm: float,
    page_height_mm: float,
) -> float:
    """Score a detection on observable evidence only.

    Three terms, all measured:

    * **coverage** -- how many candidate boxes matched the winning size cluster
      against the ``rows * cols`` the cluster's positions imply. A full grid
      scores 1.0; a cluster that explains half its own cells scores 0.5.
    * **closure** -- the mean of :func:`_axis_closure` over both axes: how
      little of the page the measured grid fails to account for.
    * **method weight** -- :data:`_METHOD_WEIGHT`, because a rectangle *is* a
      box whereas a set of rules is an inference from two.

    ``weight * (0.6 * coverage + 0.4 * closure)``. Coverage dominates because
    it is direct evidence; closure only corroborates, since a large trailing
    margin is legitimate on real stock. The line method's 0.7 weight puts it
    below :data:`LOW_CONFIDENCE_THRESHOLD` even at perfect coverage, which is
    intended: a line-derived grid always asks to be checked.
    """
    cells = rows * cols
    coverage = min(1.0, matched / cells) if cells > 0 else 0.0
    closure = (
        _axis_closure(span_x_mm, page_width_mm) + _axis_closure(span_y_mm, page_height_mm)
    ) / 2.0
    score = _METHOD_WEIGHT[method] * (_COVERAGE_WEIGHT * coverage + _CLOSURE_WEIGHT * closure)
    return round(min(1.0, max(0.0, score)), _CONFIDENCE_PRECISION)


def _span_x_mm(grid: DetectedGrid) -> float:
    """Left margin plus every column and the gaps between them."""
    return grid.margin_left_mm + grid.cols * grid.label_width_mm + (grid.cols - 1) * grid.gap_x_mm


def _span_y_mm(grid: DetectedGrid) -> float:
    """Top margin plus every row and the gaps between them."""
    return grid.margin_top_mm + grid.rows * grid.label_height_mm + (grid.rows - 1) * grid.gap_y_mm


def _rejection_reason(
    grid: DetectedGrid,
    *,
    matched: int,
    page_width_mm: float,
    page_height_mm: float,
) -> str | None:
    """Return why this detection cannot be right, or ``None`` if it can be.

    A detection that fails here is thrown away. Returning it with a low
    confidence would not be enough: the user cannot see that 3 columns of
    75mm on a 215mm page means the gap and the label were swapped, and the
    first evidence would be a ruined sheet of stock.
    """
    if grid.rows < 1 or grid.cols < 1:
        return f"a {grid.cols}x{grid.rows} grid has no cells"
    cells = grid.rows * grid.cols
    if cells > MAX_GRID_CELLS:
        return (
            f"a {grid.cols}x{grid.rows} grid has {cells} cells, over the {MAX_GRID_CELLS} maximum"
        )

    if grid.label_width_mm < _MIN_LABEL_MM or grid.label_height_mm < _MIN_LABEL_MM:
        return (
            f"the measured label is {grid.label_width_mm:.2f} x {grid.label_height_mm:.2f}mm, "
            "too small to be a label"
        )
    if (
        grid.label_width_mm > page_width_mm + _SPAN_TOLERANCE_MM
        or grid.label_height_mm > page_height_mm + _SPAN_TOLERANCE_MM
    ):
        return (
            f"the measured label is {grid.label_width_mm:.2f} x {grid.label_height_mm:.2f}mm, "
            f"larger than the {page_width_mm:.2f} x {page_height_mm:.2f}mm page"
        )

    if grid.margin_left_mm < 0.0 or grid.margin_top_mm < 0.0:
        return "the measured margins are negative, so the grid starts off the page"

    # A gap wider than the label it separates is the classic inversion: the
    # label size and the gap were read off the wrong intervals.
    if grid.gap_x_mm >= grid.label_width_mm:
        return (
            f"the horizontal gap ({grid.gap_x_mm:.2f}mm) is not smaller than the label width "
            f"({grid.label_width_mm:.2f}mm); the label and the gap were read the wrong way round"
        )
    if grid.gap_y_mm >= grid.label_height_mm:
        return (
            f"the vertical gap ({grid.gap_y_mm:.2f}mm) is not smaller than the label height "
            f"({grid.label_height_mm:.2f}mm); the label and the gap were read the wrong way round"
        )

    span_x, span_y = _span_x_mm(grid), _span_y_mm(grid)
    if span_x > page_width_mm + _SPAN_TOLERANCE_MM:
        return f"{grid.cols} column(s) span {span_x:.2f}mm across a {page_width_mm:.2f}mm page"
    if span_y > page_height_mm + _SPAN_TOLERANCE_MM:
        return f"{grid.rows} row(s) span {span_y:.2f}mm down a {page_height_mm:.2f}mm page"

    if matched < _MIN_COVERAGE * cells:
        return (
            f"only {matched} of the {cells} cells a {grid.cols}x{grid.rows} grid implies were "
            "found, which is too few to call it a grid"
        )
    return None


def _build_grid(
    *,
    method: DetectionMethod,
    rows: int,
    cols: int,
    label_width_pt: float,
    label_height_pt: float,
    margin_left_pt: float,
    margin_top_pt: float,
    gap_x_pt: float,
    gap_y_pt: float,
    matched: int,
    page_width_mm: float,
    page_height_mm: float,
) -> DetectedGrid | None:
    """Convert a measurement in points to a scored :class:`DetectedGrid`.

    ``None`` means a value would not convert -- a non-finite coordinate that
    survived extraction -- which is a discard, not an error.
    """
    try:
        grid = DetectedGrid(
            rows=rows,
            cols=cols,
            label_width_mm=quantize(pt_to_mm(label_width_pt)),
            label_height_mm=quantize(pt_to_mm(label_height_pt)),
            margin_left_mm=quantize(pt_to_mm(margin_left_pt)),
            margin_top_mm=quantize(pt_to_mm(margin_top_pt)),
            gap_x_mm=quantize(pt_to_mm(gap_x_pt)),
            gap_y_mm=quantize(pt_to_mm(gap_y_pt)),
            method=method,
            confidence=0.0,
        )
    except ValueError:
        return None

    # Scoring needs the spans, and the spans are properties of the converted
    # grid, so the grid is built once unscored and then scored.
    return replace(
        grid,
        confidence=_confidence(
            method=method,
            matched=matched,
            rows=rows,
            cols=cols,
            span_x_mm=_span_x_mm(grid),
            span_y_mm=_span_y_mm(grid),
            page_width_mm=page_width_mm,
            page_height_mm=page_height_mm,
        ),
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _detect_from_boxes(
    boxes: tuple[_Box, ...],
    *,
    method: DetectionMethod,
    page_width_mm: float,
    page_height_mm: float,
) -> tuple[DetectedGrid | None, list[str]]:
    """Find a grid among same-sized boxes. Returns the grid and any discards."""
    rejections: list[str] = []
    usable = [
        box for box in boxes if box.width >= _MIN_BOX_SIZE_PT and box.height >= _MIN_BOX_SIZE_PT
    ]
    if len(usable) < _MIN_CANDIDATE_BOXES:
        return None, rejections

    histogram = Counter(
        (
            round(box.width / _CLUSTER_TOLERANCE_PT) * _CLUSTER_TOLERANCE_PT,
            round(box.height / _CLUSTER_TOLERANCE_PT) * _CLUSTER_TOLERANCE_PT,
        )
        for box in usable
    )

    for (width_pt, height_pt), _count in histogram.most_common(_MAX_SIZE_CLUSTERS):
        members = [
            box
            for box in usable
            if abs(box.width - width_pt) <= _CLUSTER_TOLERANCE_PT
            and abs(box.height - height_pt) <= _CLUSTER_TOLERANCE_PT
        ]
        if len(members) < _MIN_CANDIDATE_BOXES:
            continue

        label_width_pt = fmean(box.width for box in members)
        label_height_pt = fmean(box.height for box in members)
        columns = _cluster(box.x0 for box in members)
        rows = _cluster(box.top for box in members)
        if not columns or not rows:
            continue

        grid = _build_grid(
            method=method,
            rows=len(rows),
            cols=len(columns),
            label_width_pt=label_width_pt,
            label_height_pt=label_height_pt,
            margin_left_pt=min(columns),
            margin_top_pt=min(rows),
            gap_x_pt=_median_gap(columns, label_width_pt),
            gap_y_pt=_median_gap(rows, label_height_pt),
            matched=len(members),
            page_width_mm=page_width_mm,
            page_height_mm=page_height_mm,
        )
        if grid is None:
            continue

        reason = _rejection_reason(
            grid,
            matched=len(members),
            page_width_mm=page_width_mm,
            page_height_mm=page_height_mm,
        )
        if reason is None:
            return grid, rejections
        rejections.append(f"a candidate grid from the page {method} was discarded: {reason}")

    return None, rejections


def _prominent_line_positions(lines: tuple[_Box, ...], *, vertical: bool) -> list[float]:
    """Positions of the full-length rules on one axis, longest-first filtered.

    Short rules -- underlines inside a label, tick marks -- are dropped by
    comparing each cluster's total length against the longest cluster found,
    which is what separates a grid boundary from page decoration.
    """
    weighted: list[tuple[float, float]] = []
    for line in lines:
        if vertical:
            if line.width > _CLUSTER_TOLERANCE_PT or line.height < _MIN_LINE_LENGTH_PT:
                continue
            weighted.append((line.x0 + line.width / 2.0, line.height))
        else:
            if line.height > _CLUSTER_TOLERANCE_PT or line.width < _MIN_LINE_LENGTH_PT:
                continue
            weighted.append((line.top + line.height / 2.0, line.width))

    clusters = _cluster_weighted(weighted)
    if len(clusters) < _MIN_BOUNDARIES:
        return []
    longest = max(weight for _, weight in clusters)
    threshold = max(_MIN_LINE_LENGTH_PT, longest * _PROMINENCE_RATIO)
    return sorted(position for position, weight in clusters if weight >= threshold)


def _infer_axis(positions: list[float]) -> tuple[int, float, float] | None:
    """Read ``(count, label size, gap)`` off a sorted list of boundaries.

    The widest repeated interval is taken as the label and a narrower repeated
    interval as the gap. That assumption is why the caller must still run
    :func:`_rejection_reason`: on a sheet whose gaps exceed its labels this
    picks them the wrong way round, and the check downstream catches it.
    """
    if len(positions) < _MIN_BOUNDARIES:
        return None
    intervals = [
        right - left
        for left, right in pairwise(positions)
        if right - left > _CLUSTER_TOLERANCE_PT / 2.0
    ]
    if not intervals:
        return None

    clusters = _cluster_sizes(intervals)
    if not clusters:
        return None

    label_size_pt = max(size for size, _ in clusters)
    count = sum(
        1 for interval in intervals if abs(interval - label_size_pt) <= _CLUSTER_TOLERANCE_PT
    )
    gaps = [size for size, _ in clusters if size < label_size_pt * _GAP_RATIO]
    if count < 1:
        return None
    return count, label_size_pt, max(gaps) if gaps else 0.0


def _detect_from_lines(
    vectors: _PageVectors,
    *,
    page_width_mm: float,
    page_height_mm: float,
) -> tuple[DetectedGrid | None, list[str]]:
    """Infer a grid from ruled boundaries. The weakest of the three methods."""
    verticals = _prominent_line_positions(vectors.lines, vertical=True)
    horizontals = _prominent_line_positions(vectors.lines, vertical=False)
    x_axis = _infer_axis(verticals)
    y_axis = _infer_axis(horizontals)
    if x_axis is None or y_axis is None:
        return None, []

    cols, label_width_pt, gap_x_pt = x_axis
    rows, label_height_pt, gap_y_pt = y_axis
    grid = _build_grid(
        method="lines",
        rows=rows,
        cols=cols,
        label_width_pt=label_width_pt,
        label_height_pt=label_height_pt,
        margin_left_pt=min(verticals),
        margin_top_pt=min(horizontals),
        gap_x_pt=gap_x_pt,
        gap_y_pt=gap_y_pt,
        matched=rows * cols,
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
    )
    if grid is None:
        return None, []

    reason = _rejection_reason(
        grid,
        matched=rows * cols,
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
    )
    if reason is None:
        return grid, []
    return None, [f"a candidate grid from the page rules was discarded: {reason}"]


def _analyse(pdf_bytes: bytes) -> _Analysis:
    """Run every detector in order of trustworthiness and keep the first hit."""
    vectors = _with_deadline(
        lambda: _extract_page_vectors(pdf_bytes), what="reading the PDF page contents"
    )
    page_width_mm = quantize(pt_to_mm(vectors.width_pt))
    page_height_mm = quantize(pt_to_mm(vectors.height_pt))

    warnings: list[str] = []
    sources: tuple[tuple[DetectionMethod, tuple[_Box, ...]], ...] = (
        ("rectangles", vectors.rects),
        ("curves", vectors.curves),
    )
    for method, boxes in sources:
        grid, rejections = _detect_from_boxes(
            boxes,
            method=method,
            page_width_mm=page_width_mm,
            page_height_mm=page_height_mm,
        )
        warnings.extend(rejections)
        if grid is not None:
            return _Analysis(grid, page_width_mm, page_height_mm, warnings)

    grid, rejections = _detect_from_lines(
        vectors, page_width_mm=page_width_mm, page_height_mm=page_height_mm
    )
    warnings.extend(rejections)
    return _Analysis(grid, page_width_mm, page_height_mm, warnings)


def detect_grid(pdf_bytes: bytes) -> DetectedGrid | None:
    """Measure the label grid on page 1, or return ``None``.

    ``None`` covers both "nothing grid-shaped was found" and "what was found
    could not be right". :func:`import_template` reports the difference;
    this function deliberately does not return a grid it cannot stand behind.

    Raises:
        ValidationError: if the bytes are not a readable, unencrypted PDF.
        LimitExceeded: if a size, page-count, object-count or time limit is hit.
        ConfigurationError: if pdfplumber is not installed.
    """
    _check_pdf_bytes(pdf_bytes)
    return _analyse(pdf_bytes).grid


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _resolve(
    explicit: _Number | None, detected: _Number | None, preset: _Number | None
) -> _Number | None:
    """First value that was actually supplied, in precedence order.

    Written out rather than with ``or`` on purpose: a legitimate ``0`` margin
    or gap is falsey, and an ``or`` chain silently replaces it with the next
    candidate. That bug shifted every column on an imported sheet.
    """
    if explicit is not None:
        return explicit
    if detected is not None:
        return detected
    return preset


def _checked_length(value: float | None, name: str) -> float | None:
    """Validate a caller-supplied millimetre value.

    Raises:
        ValidationError: if it is not a finite, non-negative number.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a number") from exc
    if not _is_finite(numeric):
        raise ValidationError(f"{name} must be a finite number")
    if numeric < 0.0:
        raise ValidationError(f"{name} must not be negative")
    return numeric


def _checked_count(value: int | None, name: str) -> int | None:
    """Validate a caller-supplied row or column count.

    Raises:
        ValidationError: if it is not an integer of at least 1.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    if value < 1:
        raise ValidationError(f"{name} must be at least 1")
    return value


def _source_name(name: str | None) -> str:
    """Reduce a caller-supplied name to a bare label.

    ``imported_from`` is echoed back to whoever reads the template, so it must
    never carry a filesystem path: directory components are stripped here, not
    trusted to have been stripped by the caller.
    """
    raw = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
    if raw.lower().endswith(_PDF_SUFFIX):
        raw = raw[: -len(_PDF_SUFFIX)]
    cleaned = "".join(character for character in raw if character.isprintable()).strip()
    return (cleaned or _DEFAULT_TEMPLATE_NAME)[:MAX_NAME_LENGTH]


def import_template(
    pdf_bytes: bytes,
    *,
    name: str | None = None,
    preset_code: str | None = None,
    rows: int | None = None,
    cols: int | None = None,
    label_width_mm: float | None = None,
    label_height_mm: float | None = None,
    margin_left_mm: float | None = None,
    margin_top_mm: float | None = None,
    gap_x_mm: float | None = None,
    gap_y_mm: float | None = None,
) -> ImportReport:
    """Build a template from a PDF, from detection and whatever the caller adds.

    Each value is resolved by precedence: an explicit argument wins, then the
    detection, then the named Avery preset. Anything still missing is named in
    the error, by the flag that would supply it.

    Args:
        pdf_bytes: The PDF to measure. Bytes, never a path.
        name: Template name, also recorded as ``imported_from``. Any directory
            component is stripped.
        preset_code: Avery product code to fall back on, e.g. ``"5160"``.
        rows: Explicit row count, overriding detection.
        cols: Explicit column count, overriding detection.
        label_width_mm: Explicit label width, overriding detection.
        label_height_mm: Explicit label height, overriding detection.
        margin_left_mm: Explicit left margin, overriding detection.
        margin_top_mm: Explicit top margin, overriding detection.
        gap_x_mm: Explicit horizontal gap, overriding detection.
        gap_y_mm: Explicit vertical gap, overriding detection.

    Returns:
        An :class:`ImportReport`. Read its ``warnings`` before printing a run.

    Raises:
        ValidationError: if the bytes are not a readable PDF, or an argument is
            not a sane number.
        TemplateError: if the grid cannot be determined, or the resolved
            geometry does not fit the page.
        NotFoundError: if ``preset_code`` names no known preset.
        LimitExceeded: if a size, page-count, object-count or time limit is hit.
        ConfigurationError: if pdfplumber or pypdf is not installed.
    """
    _check_pdf_bytes(pdf_bytes)

    rows = _checked_count(rows, "rows")
    cols = _checked_count(cols, "cols")
    label_width_mm = _checked_length(label_width_mm, "label_width_mm")
    label_height_mm = _checked_length(label_height_mm, "label_height_mm")
    margin_left_mm = _checked_length(margin_left_mm, "margin_left_mm")
    margin_top_mm = _checked_length(margin_top_mm, "margin_top_mm")
    gap_x_mm = _checked_length(gap_x_mm, "gap_x_mm")
    gap_y_mm = _checked_length(gap_y_mm, "gap_y_mm")

    page_width_mm, page_height_mm = read_page_size_mm(pdf_bytes)
    analysis = _analyse(pdf_bytes)
    warnings = list(analysis.warnings)
    detected = analysis.grid

    # The two parsers must agree on the page before a measurement taken in one
    # may be written into a template sized by the other. They disagree when the
    # page is rotated or cropped, and the result is a transposed template that
    # looks entirely plausible.
    if detected is not None and (
        abs(analysis.page_width_mm - page_width_mm) > _PAGE_MATCH_TOLERANCE_MM
        or abs(analysis.page_height_mm - page_height_mm) > _PAGE_MATCH_TOLERANCE_MM
    ):
        warnings.append(
            f"the page box says {page_width_mm:.2f} x {page_height_mm:.2f}mm but the page "
            f"contents were measured on {analysis.page_width_mm:.2f} x "
            f"{analysis.page_height_mm:.2f}mm, so the page is rotated or cropped; the "
            "automatic detection was discarded"
        )
        detected = None

    preset = get_preset(preset_code) if preset_code is not None else None
    if preset is not None and (
        abs(preset.page_width_mm - page_width_mm) > _SPAN_TOLERANCE_MM
        or abs(preset.page_height_mm - page_height_mm) > _SPAN_TOLERANCE_MM
    ):
        warnings.append(
            f"preset {preset.code} is for a {preset.page_width_mm:.2f} x "
            f"{preset.page_height_mm:.2f}mm sheet but this PDF page is "
            f"{page_width_mm:.2f} x {page_height_mm:.2f}mm"
        )

    resolved = _resolve_grid(
        detected=detected,
        preset=preset,
        rows=rows,
        cols=cols,
        label_width_mm=label_width_mm,
        label_height_mm=label_height_mm,
        margin_left_mm=margin_left_mm,
        margin_top_mm=margin_top_mm,
        gap_x_mm=gap_x_mm,
        gap_y_mm=gap_y_mm,
        warnings=warnings,
    )

    if detected is None:
        warnings.append(
            "no label grid could be measured from the PDF; the template was built from "
            "the values supplied instead"
        )
    elif detected.confidence < LOW_CONFIDENCE_THRESHOLD:
        warnings.append(
            f"the grid was detected from the page {detected.method} with confidence "
            f"{detected.confidence:.2f}, below {LOW_CONFIDENCE_THRESHOLD:.2f}; print one "
            "sheet and check it against the physical stock before a full run"
        )

    source_name = _source_name(name)
    template = _build_template(
        name=source_name,
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
        resolved=resolved,
        metadata={
            "imported_from": source_name,
            "import_source_format": "pdf",
            "auto_detected": detected is not None,
            "auto_detected_method": None if detected is None else detected.method,
            "auto_detected_confidence": None if detected is None else detected.confidence,
            "preset_code": None if preset_code is None else normalize_code(preset_code),
        },
    )

    report = validate_geometry(template)
    if report.errors:
        first = report.errors[0]
        raise TemplateError(
            f"the imported geometry is not printable: {first.message}",
            loc=("grid",),
            details=[issue.to_dict() for issue in report.errors],
        )
    warnings.extend(issue.message for issue in report.warnings)

    return ImportReport(
        template=template,
        detected=detected,
        page_size_mm=(page_width_mm, page_height_mm),
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class _ResolvedGrid:
    """The grid values after precedence has been applied. All present."""

    rows: int
    cols: int
    label_width_mm: float
    label_height_mm: float
    margin_left_mm: float
    margin_top_mm: float
    gap_x_mm: float
    gap_y_mm: float


def _resolve_grid(
    *,
    detected: DetectedGrid | None,
    preset: AveryPreset | None,
    rows: int | None,
    cols: int | None,
    label_width_mm: float | None,
    label_height_mm: float | None,
    margin_left_mm: float | None,
    margin_top_mm: float | None,
    gap_x_mm: float | None,
    gap_y_mm: float | None,
    warnings: list[str],
) -> _ResolvedGrid:
    """Apply argument -> detection -> preset precedence to every grid value.

    Raises:
        TemplateError: naming the flags that are still needed.
    """
    resolved_rows = _resolve(
        rows,
        None if detected is None else detected.rows,
        None if preset is None else preset.rows,
    )
    resolved_cols = _resolve(
        cols,
        None if detected is None else detected.cols,
        None if preset is None else preset.cols,
    )
    resolved_width = _resolve(
        label_width_mm,
        None if detected is None else detected.label_width_mm,
        None if preset is None else preset.label_width_mm,
    )
    resolved_height = _resolve(
        label_height_mm,
        None if detected is None else detected.label_height_mm,
        None if preset is None else preset.label_height_mm,
    )

    missing: list[str] = []
    if resolved_rows is None:
        missing.append("--rows")
    if resolved_cols is None:
        missing.append("--cols")
    if resolved_width is None:
        missing.append("--label-width-mm")
    if resolved_height is None:
        missing.append("--label-height-mm")
    if (
        resolved_rows is None
        or resolved_cols is None
        or resolved_width is None
        or resolved_height is None
    ):
        raise TemplateError(
            "the label grid could not be measured from this PDF; supply "
            + ", ".join(missing)
            + ", or --template-code for a known Avery stock"
        )

    resolved_margin_left = _resolve(
        margin_left_mm,
        None if detected is None else detected.margin_left_mm,
        None if preset is None else preset.margin_left_mm,
    )
    resolved_margin_top = _resolve(
        margin_top_mm,
        None if detected is None else detected.margin_top_mm,
        None if preset is None else preset.margin_top_mm,
    )
    resolved_gap_x = _resolve(
        gap_x_mm,
        None if detected is None else detected.gap_x_mm,
        None if preset is None else preset.gap_x_mm,
    )
    resolved_gap_y = _resolve(
        gap_y_mm,
        None if detected is None else detected.gap_y_mm,
        None if preset is None else preset.gap_y_mm,
    )

    # Margins and gaps are the only values with a defensible default, but
    # defaulting them silently is how a sheet prints 5mm off. Say so.
    defaulted = [
        flag
        for flag, value in (
            ("--margin-left-mm", resolved_margin_left),
            ("--margin-top-mm", resolved_margin_top),
            ("--gap-x-mm", resolved_gap_x),
            ("--gap-y-mm", resolved_gap_y),
        )
        if value is None
    ]
    if defaulted:
        warnings.append(
            "no value was detected or supplied for " + ", ".join(defaulted) + "; assuming 0mm"
        )

    return _ResolvedGrid(
        rows=resolved_rows,
        cols=resolved_cols,
        label_width_mm=resolved_width,
        label_height_mm=resolved_height,
        margin_left_mm=0.0 if resolved_margin_left is None else resolved_margin_left,
        margin_top_mm=0.0 if resolved_margin_top is None else resolved_margin_top,
        gap_x_mm=0.0 if resolved_gap_x is None else resolved_gap_x,
        gap_y_mm=0.0 if resolved_gap_y is None else resolved_gap_y,
    )


def _build_template(
    *,
    name: str,
    page_width_mm: float,
    page_height_mm: float,
    resolved: _ResolvedGrid,
    metadata: dict[str, Any],
) -> LabelTemplate:
    """Assemble and validate the template document.

    The trailing margins are derived from what the grid leaves over and
    clamped at zero: a negative one would be rejected by the schema, and the
    overrun it represents is reported by
    :func:`label_sheet_generator.geometry.validate` with a message that says
    by how much.

    Raises:
        TemplateError: if the values do not satisfy the template schema.
    """
    span_x = (
        resolved.margin_left_mm
        + resolved.cols * resolved.label_width_mm
        + (resolved.cols - 1) * resolved.gap_x_mm
    )
    span_y = (
        resolved.margin_top_mm
        + resolved.rows * resolved.label_height_mm
        + (resolved.rows - 1) * resolved.gap_y_mm
    )

    try:
        return LabelTemplate.model_validate(
            {
                "template_type": "label",
                "name": name,
                "page": {
                    "width_mm": quantize(page_width_mm),
                    "height_mm": quantize(page_height_mm),
                },
                "grid": {
                    "rows": resolved.rows,
                    "cols": resolved.cols,
                    "margin_left_mm": quantize(resolved.margin_left_mm),
                    "margin_top_mm": quantize(resolved.margin_top_mm),
                    "margin_right_mm": quantize(max(0.0, page_width_mm - span_x)),
                    "margin_bottom_mm": quantize(max(0.0, page_height_mm - span_y)),
                    "gap_x_mm": quantize(resolved.gap_x_mm),
                    "gap_y_mm": quantize(resolved.gap_y_mm),
                    "label_width_mm": quantize(resolved.label_width_mm),
                    "label_height_mm": quantize(resolved.label_height_mm),
                },
                "metadata": metadata,
            }
        )
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        raise TemplateError(
            f"the imported values do not make a valid template: {first.get('msg')}",
            loc=tuple(str(part) for part in first.get("loc", ())),
        ) from exc
    except ValueError as exc:
        raise TemplateError(f"the imported values do not make a valid template: {exc}") from exc
