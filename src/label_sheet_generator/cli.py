from __future__ import annotations

import argparse
import sys
from pathlib import Path

from label_sheet_generator.avery import build_template_from_avery_preset, iter_avery_preset_groups
from label_sheet_generator.io import load_records, load_template, load_text_layout_template, save_template
from label_sheet_generator.models import LabelTemplate, TemplateError, TextLayoutTemplate
from label_sheet_generator.presets import (
    iter_local_templates,
    resolve_template_input_path,
    resolve_template_output_path,
)
from label_sheet_generator.rendering import render_pdf
from label_sheet_generator.template_import import import_template_from_pdf
from label_sheet_generator.units import in_to_mm
from label_sheet_generator.web_launcher import main as launch_web_ui


def _add_length_pair_argument(parser: argparse.ArgumentParser, option_name: str, help_text: str) -> None:
    destination = option_name.replace("-", "_")
    parser.add_argument(
        f"--{option_name}-mm",
        dest=f"{destination}_mm",
        type=float,
        help=f"{help_text} in millimeters",
    )
    parser.add_argument(
        f"--{option_name}-in",
        dest=f"{destination}_in",
        type=float,
        help=f"{help_text} in inches",
    )


def _resolve_length_override(option_name: str, millimeters: float | None, inches: float | None) -> float | None:
    if millimeters is not None and inches is not None:
        raise TemplateError(
            f"{option_name} accepts either millimeters or inches, not both"
        )
    if millimeters is not None:
        return millimeters
    if inches is not None:
        return in_to_mm(inches)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="label-sheet",
        description="Generate printable label sheets as PDFs from structured templates and record data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate",
        help="render a template into a print-ready PDF",
    )
    generate_parser.add_argument(
        "template",
        help="path to a label/full template file or a bare template name resolved from templates/",
    )
    generate_parser.add_argument("output", help="target PDF path")
    generate_parser.add_argument(
        "--records",
        help="optional JSON record file; when omitted, static template values are rendered once",
    )
    generate_parser.add_argument(
        "--layout-template",
        help="optional text layout template resolved from templates/; its elements replace the base template elements",
    )
    generate_parser.add_argument(
        "--draw-border",
        "--draw-borders",
        "--outline-slots",
        dest="outline_slots",
        action="store_true",
        help="draw faint borders around each label slot for layout debugging",
    )
    generate_parser.add_argument(
        "--orientation",
        choices=["portrait", "landscape"],
        default="portrait",
        help="output page orientation; landscape rotates the rendered label sheet by 90 degrees",
    )
    generate_parser.add_argument(
        "--page-rotation",
        choices=[0, 90, 180, 270],
        type=int,
        default=0,
        help="rotate the final PDF page by 0, 90, 180, or 270 degrees after rendering",
    )
    generate_parser.add_argument(
        "--text-rotation",
        type=float,
        help="rotate all text elements by the given number of degrees; overrides any rotation_deg values in the template",
    )

    import_parser = subparsers.add_parser(
        "import-template",
        help="convert a PDF label sheet into a reusable JSON template",
    )
    import_parser.add_argument("source", help="source PDF path")
    import_parser.add_argument(
        "output",
        help="output template path (.json); bare filenames are stored in templates/labels/",
    )
    import_parser.add_argument(
        "--template-code",
        dest="template_code",
        help="optional Avery template code used as fallback geometry when PDF auto-detection is incomplete",
    )
    import_parser.add_argument(
        "--preset",
        dest="template_code",
        help=argparse.SUPPRESS,
    )
    import_parser.add_argument("--rows", type=int, help="override or provide the label row count")
    import_parser.add_argument("--cols", type=int, help="override or provide the label column count")
    _add_length_pair_argument(import_parser, "label-width", "override or provide the label width")
    _add_length_pair_argument(import_parser, "label-height", "override or provide the label height")
    _add_length_pair_argument(import_parser, "margin-left", "override or provide the left margin")
    _add_length_pair_argument(import_parser, "margin-top", "override or provide the top margin")
    _add_length_pair_argument(import_parser, "gap-x", "override or provide the horizontal gap")
    _add_length_pair_argument(import_parser, "gap-y", "override or provide the vertical gap")

    template_parser = subparsers.add_parser(
        "avery-template",
        help="create a reusable template skeleton from a built-in Avery template",
    )
    template_parser.add_argument("code", help="Avery product code, such as 5160 or 8163")
    template_parser.add_argument(
        "output",
        help="output template path (.json); bare filenames are stored in templates/labels/",
    )
    template_parser.add_argument("--name", help="optional template name")

    subparsers.add_parser(
        "list-templates",
        aliases=["list-presets"],
        help="list built-in Avery templates plus local label and text layout templates stored in templates/",
    )

    ui_parser = subparsers.add_parser(
        "ui",
        help="launch the FastAPI web UI backend",
    )
    ui_parser.add_argument("--host", default="127.0.0.1", help="host interface for the web UI backend")
    ui_parser.add_argument("--port", type=int, default=8000, help="port for the web UI backend")
    ui_parser.add_argument("--reload", action="store_true", help="reload the backend when Python files change")

    return parser


def _apply_layout_template(
    label_template: LabelTemplate,
    layout_template: TextLayoutTemplate,
    *,
    layout_path: Path,
) -> LabelTemplate:
    metadata = dict(label_template.metadata)
    metadata["layout_template"] = layout_template.name or layout_path.stem
    return LabelTemplate(
        page=label_template.page,
        grid=label_template.grid,
        elements=list(layout_template.elements),
        name=label_template.name,
        metadata=metadata,
    )


def _run_generate(args: argparse.Namespace) -> int:
    template_path = resolve_template_input_path(args.template)
    output_path = Path(args.output)
    records_path = Path(args.records) if args.records else None

    template = load_template(template_path)
    render_template_path = template_path
    if args.layout_template:
        layout_path = resolve_template_input_path(args.layout_template)
        layout_template = load_text_layout_template(layout_path)
        template = _apply_layout_template(template, layout_template, layout_path=layout_path)
        render_template_path = layout_path

    records = load_records(records_path) if records_path else []
    render_pdf(
        template,
        records,
        output_path,
        template_path=render_template_path,
        records_path=records_path,
        outline_slots=args.outline_slots,
        page_orientation=args.orientation,
        page_rotation_deg=args.page_rotation,
        text_rotation_deg=args.text_rotation,
    )
    return 0


def _run_import(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    if source_path.suffix.lower() != ".pdf":
        raise TemplateError(
            "only PDF import is supported; .doc, .psd, and .ai are not reliable inputs for a Python-only CLI"
        )

    label_width_mm = _resolve_length_override("--label-width", args.label_width_mm, args.label_width_in)
    label_height_mm = _resolve_length_override("--label-height", args.label_height_mm, args.label_height_in)
    margin_left_mm = _resolve_length_override("--margin-left", args.margin_left_mm, args.margin_left_in)
    margin_top_mm = _resolve_length_override("--margin-top", args.margin_top_mm, args.margin_top_in)
    gap_x_mm = _resolve_length_override("--gap-x", args.gap_x_mm, args.gap_x_in)
    gap_y_mm = _resolve_length_override("--gap-y", args.gap_y_mm, args.gap_y_in)

    template = import_template_from_pdf(
        source_path,
        preset_code=args.template_code,
        rows=args.rows,
        cols=args.cols,
        label_width_mm=label_width_mm,
        label_height_mm=label_height_mm,
        margin_left_mm=margin_left_mm,
        margin_top_mm=margin_top_mm,
        gap_x_mm=gap_x_mm,
        gap_y_mm=gap_y_mm,
    )
    save_template(resolve_template_output_path(args.output, template_type="label"), template)
    return 0


def _run_avery_template(args: argparse.Namespace) -> int:
    template = build_template_from_avery_preset(args.code, name=args.name)
    save_template(resolve_template_output_path(args.output, template_type="label"), template)
    return 0


def _run_list_templates() -> int:
    for template in iter_avery_preset_groups():
        codes = ", ".join(template.codes)
        print(f"{codes}: {template.description}")

    local_templates = iter_local_templates()
    local_label_templates = [template for template in local_templates if template.template_type == "label"]
    local_text_layout_templates = [template for template in local_templates if template.template_type == "text-layout"]

    if local_label_templates:
        print("local label templates:")
        for template in local_label_templates:
            description = template.template_name or template.key
            print(f"{template.key}: {description} ({template.path.as_posix()})")
    if local_text_layout_templates:
        print("local text layout templates:")
        for template in local_text_layout_templates:
            description = template.template_name or template.key
            print(f"{template.key}: {description} ({template.path.as_posix()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "generate":
            return _run_generate(args)
        if args.command == "import-template":
            return _run_import(args)
        if args.command == "avery-template":
            return _run_avery_template(args)
        if args.command in {"list-templates", "list-presets"}:
            return _run_list_templates()
        if args.command == "ui":
            return launch_web_ui(host=args.host, port=args.port, reload=args.reload)
    except TemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1

    print(f"unsupported command: {args.command}", file=sys.stderr)
    return 2
