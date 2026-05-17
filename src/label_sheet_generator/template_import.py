from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any

import pdfplumber
from pypdf import PdfReader

from label_sheet_generator.avery import get_avery_preset, normalize_avery_code
from label_sheet_generator.models import GridSpec, LabelTemplate, PageSpec, TemplateError
from label_sheet_generator.units import pt_to_mm


@dataclass(slots=True)
class DetectedGrid:
    rows: int
    cols: int
    label_width_mm: float
    label_height_mm: float
    margin_left_mm: float
    margin_top_mm: float
    gap_x_mm: float
    gap_y_mm: float
    method: str


def _cluster(values: list[float], tolerance_pt: float = 2.0) -> list[float]:
    if not values:
        return []

    buckets: list[list[float]] = []
    for value in sorted(values):
        if not buckets or abs(value - buckets[-1][-1]) > tolerance_pt:
            buckets.append([value])
        else:
            buckets[-1].append(value)
    return [mean(bucket) for bucket in buckets]


def _median_gap(values: list[float], size: float) -> float:
    if len(values) <= 1:
        return 0.0
    deltas = [right - left - size for left, right in zip(values, values[1:])]
    return max(median(deltas), 0.0)


def _cluster_weighted(values: list[tuple[float, float]], tolerance_pt: float = 2.0) -> list[tuple[float, float]]:
    if not values:
        return []

    buckets: list[dict[str, Any]] = []
    for position, weight in sorted(values, key=lambda item: item[0]):
        if not buckets or abs(position - buckets[-1]["positions"][-1]) > tolerance_pt:
            buckets.append({"positions": [position], "weight": weight})
        else:
            buckets[-1]["positions"].append(position)
            buckets[-1]["weight"] += weight
    return [(mean(bucket["positions"]), float(bucket["weight"])) for bucket in buckets]


def _cluster_sizes(values: list[float], tolerance_pt: float = 2.0) -> list[tuple[float, int]]:
    if not values:
        return []

    buckets: list[list[float]] = []
    for value in sorted(values):
        if not buckets or abs(value - buckets[-1][-1]) > tolerance_pt:
            buckets.append([value])
        else:
            buckets[-1].append(value)
    return [(mean(bucket), len(bucket)) for bucket in buckets]


def _detect_grid_from_box_candidates(candidates: list[dict[str, Any]], *, method: str) -> DetectedGrid | None:
    box_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("width", 0.0) >= 20.0 and candidate.get("height", 0.0) >= 20.0
    ]

    if len(box_candidates) < 2:
        return None

    tolerance_pt = 2.0
    size_counts = Counter(
        (
            round(candidate["width"] / tolerance_pt) * tolerance_pt,
            round(candidate["height"] / tolerance_pt) * tolerance_pt,
        )
        for candidate in box_candidates
    )

    for (width_pt, height_pt), _ in size_counts.most_common():
        candidates = [
            candidate
            for candidate in box_candidates
            if abs(candidate["width"] - width_pt) <= tolerance_pt
            and abs(candidate["height"] - height_pt) <= tolerance_pt
        ]
        resolved_width_pt = mean(candidate["width"] for candidate in candidates)
        resolved_height_pt = mean(candidate["height"] for candidate in candidates)
        x_positions = _cluster([candidate["x0"] for candidate in candidates], tolerance_pt=tolerance_pt)
        top_positions = _cluster([candidate["top"] for candidate in candidates], tolerance_pt=tolerance_pt)

        if len(x_positions) < 1 or len(top_positions) < 1:
            continue
        if len(candidates) < 2:
            continue
        if len(candidates) < max(len(x_positions) * len(top_positions) - 1, 2):
            continue

        margin_left_pt = min(candidate["x0"] for candidate in candidates)
        margin_top_pt = min(candidate["top"] for candidate in candidates)
        return DetectedGrid(
            rows=len(top_positions),
            cols=len(x_positions),
            label_width_mm=pt_to_mm(resolved_width_pt),
            label_height_mm=pt_to_mm(resolved_height_pt),
            margin_left_mm=pt_to_mm(margin_left_pt),
            margin_top_mm=pt_to_mm(margin_top_pt),
            gap_x_mm=pt_to_mm(_median_gap(x_positions, resolved_width_pt)),
            gap_y_mm=pt_to_mm(_median_gap(top_positions, resolved_height_pt)),
            method=method,
        )

    return None


def _detect_grid_from_rectangles(page: Any) -> DetectedGrid | None:
    return _detect_grid_from_box_candidates(list(page.rects), method="rectangles")


def _detect_grid_from_curves(page: Any) -> DetectedGrid | None:
    return _detect_grid_from_box_candidates(list(page.curves), method="curves")


def _extract_prominent_line_positions(lines: list[dict[str, Any]], *, orientation: str, tolerance_pt: float = 2.0) -> list[float]:
    weighted_positions: list[tuple[float, float]] = []

    for line in lines:
        x0 = float(line.get("x0", 0.0))
        x1 = float(line.get("x1", 0.0))
        top = float(line.get("top", 0.0))
        bottom = float(line.get("bottom", top))
        width = abs(x1 - x0)
        height = abs(bottom - top)

        if orientation == "vertical":
            if width > tolerance_pt or height < 20.0:
                continue
            weighted_positions.append(((x0 + x1) / 2.0, height))
        else:
            if height > tolerance_pt or width < 20.0:
                continue
            weighted_positions.append(((top + bottom) / 2.0, width))

    clusters = _cluster_weighted(weighted_positions, tolerance_pt=tolerance_pt)
    if len(clusters) < 2:
        return []

    max_weight = max(weight for _, weight in clusters)
    min_weight = max(20.0, max_weight * 0.5)
    return sorted(position for position, weight in clusters if weight >= min_weight)


def _infer_axis_from_boundaries(boundary_positions: list[float], tolerance_pt: float = 2.0) -> tuple[int, float, float] | None:
    if len(boundary_positions) < 2:
        return None

    intervals = [
        right - left
        for left, right in zip(boundary_positions, boundary_positions[1:])
        if right - left > tolerance_pt / 2.0
    ]
    if not intervals:
        return None

    clusters = _cluster_sizes(intervals, tolerance_pt=tolerance_pt)
    if not clusters:
        return None

    label_size_pt = max(size for size, _ in clusters)
    label_count = sum(1 for interval in intervals if abs(interval - label_size_pt) <= tolerance_pt)
    gap_candidates = [size for size, _ in clusters if size < label_size_pt * 0.75]
    gap_size_pt = max(gap_candidates) if gap_candidates else 0.0

    if label_count <= 0:
        return None

    return label_count, label_size_pt, gap_size_pt


def _detect_grid_from_lines(page: Any) -> DetectedGrid | None:
    vertical_positions = _extract_prominent_line_positions(page.lines, orientation="vertical")
    horizontal_positions = _extract_prominent_line_positions(page.lines, orientation="horizontal")

    x_axis = _infer_axis_from_boundaries(vertical_positions)
    y_axis = _infer_axis_from_boundaries(horizontal_positions)
    if x_axis is None or y_axis is None:
        return None

    cols, label_width_pt, gap_x_pt = x_axis
    rows, label_height_pt, gap_y_pt = y_axis
    return DetectedGrid(
        rows=rows,
        cols=cols,
        label_width_mm=pt_to_mm(label_width_pt),
        label_height_mm=pt_to_mm(label_height_pt),
        margin_left_mm=pt_to_mm(min(vertical_positions)),
        margin_top_mm=pt_to_mm(min(horizontal_positions)),
        gap_x_mm=pt_to_mm(gap_x_pt),
        gap_y_mm=pt_to_mm(gap_y_pt),
        method="lines",
    )


def read_pdf_page_size_mm(source_path: str | Path) -> tuple[float, float]:
    reader = PdfReader(str(source_path))
    page = reader.pages[0]
    return pt_to_mm(float(page.mediabox.width)), pt_to_mm(float(page.mediabox.height))


def detect_grid_from_pdf(source_path: str | Path) -> DetectedGrid | None:
    with pdfplumber.open(str(source_path)) as pdf:
        page = pdf.pages[0]
        detected = _detect_grid_from_rectangles(page)
        if detected is not None:
            return detected
        detected = _detect_grid_from_curves(page)
        if detected is not None:
            return detected
        return _detect_grid_from_lines(page)


def import_template_from_pdf(
    source_path: str | Path,
    *,
    preset_code: str | None = None,
    rows: int | None = None,
    cols: int | None = None,
    label_width_mm: float | None = None,
    label_height_mm: float | None = None,
    margin_left_mm: float | None = None,
    margin_top_mm: float | None = None,
    gap_x_mm: float | None = None,
    gap_y_mm: float | None = None,
) -> LabelTemplate:
    page_width_mm, page_height_mm = read_pdf_page_size_mm(source_path)
    detected = detect_grid_from_pdf(source_path)
    preset = get_avery_preset(preset_code) if preset_code else None

    resolved_rows = rows or (detected.rows if detected else None) or (preset.rows if preset else None)
    resolved_cols = cols or (detected.cols if detected else None) or (preset.cols if preset else None)
    resolved_label_width_mm = (
        label_width_mm
        or (detected.label_width_mm if detected else None)
        or (preset.label_width_mm if preset else None)
    )
    resolved_label_height_mm = (
        label_height_mm
        or (detected.label_height_mm if detected else None)
        or (preset.label_height_mm if preset else None)
    )
    resolved_margin_left_mm = (
        margin_left_mm
        if margin_left_mm is not None
        else (detected.margin_left_mm if detected else (preset.margin_left_mm if preset else 0.0))
    )
    resolved_margin_top_mm = (
        margin_top_mm
        if margin_top_mm is not None
        else (detected.margin_top_mm if detected else (preset.margin_top_mm if preset else 0.0))
    )
    resolved_gap_x_mm = (
        gap_x_mm
        if gap_x_mm is not None
        else (detected.gap_x_mm if detected else (preset.gap_x_mm if preset else 0.0))
    )
    resolved_gap_y_mm = (
        gap_y_mm
        if gap_y_mm is not None
        else (detected.gap_y_mm if detected else (preset.gap_y_mm if preset else 0.0))
    )

    missing_flags: list[str] = []
    if resolved_rows is None:
        missing_flags.append("--rows")
    if resolved_cols is None:
        missing_flags.append("--cols")
    if resolved_label_width_mm is None:
        missing_flags.append("--label-width-mm or --label-width-in")
    if resolved_label_height_mm is None:
        missing_flags.append("--label-height-mm or --label-height-in")

    if missing_flags:
        raise TemplateError(
            "could not infer the label grid from the PDF; supply " + ", ".join(missing_flags)
        )

    if resolved_rows <= 0 or resolved_cols <= 0:
        raise TemplateError("rows and cols must be positive")
    if resolved_label_width_mm <= 0 or resolved_label_height_mm <= 0:
        raise TemplateError("label width and height must be positive")

    margin_right_mm = (
        page_width_mm
        - resolved_margin_left_mm
        - resolved_cols * resolved_label_width_mm
        - max(resolved_cols - 1, 0) * resolved_gap_x_mm
    )
    margin_bottom_mm = (
        page_height_mm
        - resolved_margin_top_mm
        - resolved_rows * resolved_label_height_mm
        - max(resolved_rows - 1, 0) * resolved_gap_y_mm
    )

    if margin_right_mm < -0.1 or margin_bottom_mm < -0.1:
        raise TemplateError("detected or supplied dimensions do not fit inside the PDF page size")

    return LabelTemplate(
        name=Path(source_path).stem,
        page=PageSpec(width_mm=page_width_mm, height_mm=page_height_mm),
        grid=GridSpec(
            rows=resolved_rows,
            cols=resolved_cols,
            margin_left_mm=resolved_margin_left_mm,
            margin_top_mm=resolved_margin_top_mm,
            margin_right_mm=max(margin_right_mm, 0.0),
            margin_bottom_mm=max(margin_bottom_mm, 0.0),
            gap_x_mm=resolved_gap_x_mm,
            gap_y_mm=resolved_gap_y_mm,
            label_width_mm=resolved_label_width_mm,
            label_height_mm=resolved_label_height_mm,
        ),
        metadata={
            "imported_from": str(Path(source_path).name),
            "import_source_format": "pdf",
            "auto_detected": detected is not None,
            "auto_detected_method": detected.method if detected else None,
            "preset_code": normalize_avery_code(preset_code) if preset_code else None,
        },
    )

