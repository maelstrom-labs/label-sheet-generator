"""Command line interface.

Built on the same :class:`~label_sheet_generator.service.LabelSheetService` as
the web app, so the two cannot drift.

The CLI is the one caller allowed to read and write arbitrary paths, because a
person running it in their own shell already has that access. The web layer
cannot reach these helpers: they live here, not in the service.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from label_sheet_generator import __version__
from label_sheet_generator.avery import build_template, iter_presets
from label_sheet_generator.catalog import Catalog, parse_template_document
from label_sheet_generator.errors import (
    ConfigurationError,
    LabelSheetError,
    NotFoundError,
    ValidationError,
)
from label_sheet_generator.fsio import dumps_json, loads_json, read_bytes, write_atomic
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.logging_config import quiet_noisy_libraries
from label_sheet_generator.pdfimport import MAX_PDF_BYTES, import_template
from label_sheet_generator.records import format_document, parse_document, parse_upload
from label_sheet_generator.render import RenderOptions, render_pdf
from label_sheet_generator.schema import LabelTemplate
from label_sheet_generator.service import LabelSheetService, MarginOverrides
from label_sheet_generator.settings import Settings, default_settings

#: Exit codes. Distinguishing "your input was wrong" from "this broke" lets a
#: script branch on the difference.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: A local file read by the CLI is bounded too, just far more generously than
#: an HTTP upload: a person can legitimately render a large local dataset.
CLI_MAX_INPUT_BYTES = 64 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="label-sheet",
        description=("Generate print-ready label sheet PDFs from JSON templates and record data."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--templates",
        metavar="DIR",
        help="extra directory of templates to layer over the built-ins",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="render a template and records into a PDF")
    generate.add_argument("template", help="template id from list-templates, or a .json path")
    generate.add_argument("output", help="destination .pdf path")
    generate.add_argument("--records", help="JSON or CSV record file")
    generate.add_argument("--layout", help="text layout id to swap in")
    generate.add_argument("--orientation", choices=["portrait", "landscape"], default="portrait")
    generate.add_argument("--page-rotation", type=int, choices=[0, 90, 180, 270], default=0)
    generate.add_argument("--text-rotation", type=float, help="rotate every text element")
    generate.add_argument(
        "--borders",
        action="store_true",
        help="draw a faint guide box around each label for alignment checks",
    )
    generate.add_argument("--margin-top", type=float, metavar="MM")
    generate.add_argument("--margin-right", type=float, metavar="MM")
    generate.add_argument("--margin-bottom", type=float, metavar="MM")
    generate.add_argument("--margin-left", type=float, metavar="MM")
    generate.add_argument("--assets", metavar="DIR", help="directory image elements may load from")
    generate.add_argument("--strict", action="store_true", help="fail on any per-record problem")
    generate.add_argument("--force", action="store_true", help="overwrite an existing output")

    listing = subparsers.add_parser(
        "list-templates", help="list built-in, user and Avery templates"
    )
    listing.add_argument("--json", action="store_true", dest="as_json")

    check = subparsers.add_parser("check", help="validate a template without rendering")
    check.add_argument("template", help="template id or .json path")

    preset = subparsers.add_parser(
        "avery-template", help="write a template file from an Avery product code"
    )
    preset.add_argument("code", help="product code, e.g. 5160")
    preset.add_argument("output", help="destination .json path")
    preset.add_argument("--name")
    preset.add_argument("--force", action="store_true")

    importer = subparsers.add_parser(
        "import-template", help="measure an existing label sheet PDF into a template"
    )
    importer.add_argument("source", help="source .pdf path")
    importer.add_argument("output", help="destination .json path")
    importer.add_argument("--name")
    importer.add_argument("--force", action="store_true")
    importer.add_argument(
        "--template-code",
        metavar="CODE",
        help="Avery product code to fall back on when detection cannot see a value",
    )
    importer.add_argument("--rows", type=int)
    importer.add_argument("--cols", type=int)
    importer.add_argument("--label-width-mm", type=float, metavar="MM")
    importer.add_argument("--label-height-mm", type=float, metavar="MM")
    importer.add_argument("--margin-left-mm", type=float, metavar="MM")
    importer.add_argument("--margin-top-mm", type=float, metavar="MM")
    importer.add_argument("--gap-x-mm", type=float, metavar="MM")
    importer.add_argument("--gap-y-mm", type=float, metavar="MM")

    serve = subparsers.add_parser("serve", help="run the web interface")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    return parser


def _settings(args: argparse.Namespace) -> Settings:
    base = default_settings()
    overrides: dict[str, Any] = {}
    if getattr(args, "templates", None):
        overrides["user_template_root"] = Path(args.templates).expanduser()
    if getattr(args, "assets", None):
        overrides["asset_root"] = Path(args.assets).expanduser()
    settings = base if not overrides else _replace(base, overrides)
    settings.validate()
    return settings


def _replace(settings: Settings, overrides: dict[str, Any]) -> Settings:
    return replace(settings, **overrides)


def _load_local_template(path: Path) -> LabelTemplate:
    """Read a template from an explicit path, for CLI use only."""
    if not _is_file(path):
        raise NotFoundError(f"no template file at {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{path.name} could not be read ({exc.strerror or exc})") from exc
    data = loads_json(raw, what=f"template {path.name}")
    if not isinstance(data, dict):
        raise ValidationError(f"{path.name} must contain a JSON object")
    template = parse_template_document(data, what=path.name)
    if not isinstance(template, LabelTemplate):
        raise ValidationError(
            f"{path.name} is a text layout; pass it with --layout and a label template"
        )
    return template


def _exists(candidate: Path) -> bool:
    """``Path.exists`` that answers "no" instead of raising.

    ``stat`` rejects some strings outright -- a name longer than NAME_MAX, or
    one containing a NUL -- with ``OSError``/``ValueError`` rather than
    returning False. A template *id* is free-form text, so probing it as a path
    must not be able to turn a mistyped id into an uncaught traceback.
    """
    try:
        return candidate.exists()
    except (OSError, ValueError):
        return False


def _is_file(candidate: Path) -> bool:
    """``Path.is_file`` with the same guard as :func:`_exists`."""
    try:
        return candidate.is_file()
    except (OSError, ValueError):
        return False


def _write_output(path: Path, data: bytes | str) -> None:
    """Write a result file, reporting a filesystem refusal as an error.

    An unwritable destination is an ordinary thing for a person at a shell to
    hit -- a typo puts the output inside a regular file, or the directory is
    read-only -- and it must read as one line, not as a traceback.
    """
    try:
        write_atomic(path, data)
    except OSError as exc:
        raise LabelSheetError(f"could not write {path} ({exc.strerror or exc})") from exc


def _resolve_template(service: LabelSheetService, reference: str) -> tuple[LabelTemplate, str]:
    """Accept either a catalog id or a filesystem path."""
    candidate = Path(reference)
    if candidate.suffix.lower() == ".json" or _exists(candidate):
        return _load_local_template(candidate), candidate.stem
    entry = service.catalog.get(reference)
    if not isinstance(entry.template, LabelTemplate):
        raise ValidationError(f"{reference!r} is a text layout, not a label template")
    return entry.template, entry.name


def _load_records(service: LabelSheetService, path: Path, fields: Sequence[str]) -> str:
    if not _is_file(path):
        raise NotFoundError(f"no record file at {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{path.name} could not be read ({exc.strerror or exc})") from exc
    if len(data) > CLI_MAX_INPUT_BYTES:
        raise ValidationError(f"{path.name} is larger than {CLI_MAX_INPUT_BYTES} bytes")
    report = parse_upload(
        data,
        path.name,
        fields=list(fields),
        max_records=service.settings.max_records,
        max_value_length=service.settings.max_field_value_length,
    )
    for warning in report.warnings:
        print(f"warning: {warning['message']}", file=sys.stderr)
    return format_document(
        list(fields),
        records=[dict(record) for record in report.document.records],
        schema=list(report.document.schema),
    )


def _run_generate(args: argparse.Namespace, service: LabelSheetService) -> int:
    output = Path(args.output)
    if _exists(output) and not args.force:
        raise ValidationError(f"{output} already exists; pass --force to overwrite it")
    if output.suffix.lower() != ".pdf":
        raise ValidationError("the output path must end in .pdf")

    template, template_name = _resolve_template(service, args.template)

    if args.layout:
        entry = service.catalog.get(args.layout)
        template = template.model_copy(update={"elements": list(entry.template.elements)})

    overrides = MarginOverrides(
        top_mm=args.margin_top,
        right_mm=args.margin_right,
        bottom_mm=args.margin_bottom,
        left_mm=args.margin_left,
    )
    if not overrides.is_empty():
        grid = template.grid
        template = template.model_copy(
            update={
                "grid": grid.model_copy(
                    update={
                        "margin_top_mm": _pick(overrides.top_mm, grid.margin_top_mm),
                        "margin_right_mm": _pick(overrides.right_mm, grid.margin_right_mm),
                        "margin_bottom_mm": _pick(overrides.bottom_mm, grid.margin_bottom_mm),
                        "margin_left_mm": _pick(overrides.left_mm, grid.margin_left_mm),
                    }
                )
            }
        )

    document = (
        _load_records(service, Path(args.records), template.field_names)
        if args.records
        else format_document(template.field_names)
    )
    record_document = parse_document(
        document,
        max_records=service.settings.max_records,
        max_value_length=service.settings.max_field_value_length,
    )

    report = validate_geometry(template)
    for issue in report.warnings:
        print(f"warning: {issue.message}", file=sys.stderr)
    if not report.ok:
        raise ValidationError(report.errors[0].message)

    result = render_pdf(
        template,
        [dict(record) for record in record_document.records],
        options=RenderOptions(
            page_orientation=args.orientation,
            page_rotation_deg=args.page_rotation,
            text_rotation_deg=args.text_rotation,
            outline_slots=args.borders,
            strict=args.strict,
        ),
        assets=service.assets,
        max_pages=None,
        max_output_bytes=None,
    )
    for warning in result.warnings:
        print(f"warning: {warning.message}", file=sys.stderr)

    _write_output(output, result.pdf_bytes)
    print(
        f"wrote {output} - {result.label_count} label(s) on {result.page_count} page(s) "
        f"from {template_name}"
    )
    return EXIT_OK


def _run_list(args: argparse.Namespace, service: LabelSheetService) -> int:
    catalog = service.catalog
    if args.as_json:
        payload = {
            "templates": [entry.to_dict() for entry in catalog.label_templates()],
            "layouts": [entry.to_dict() for entry in catalog.layout_templates()],
            "broken": [entry.to_dict() for entry in catalog.broken],
        }
        print(dumps_json(payload), end="")
        return EXIT_OK

    print("Label templates:")
    for entry in catalog.label_templates():
        detail = ""
        if entry.geometry:
            detail = (
                f"  {entry.geometry['cols']}x{entry.geometry['rows']}"
                f" - {entry.labels_per_page}/sheet"
            )
        print(f"  {entry.id:<28} {entry.name}{detail}")

    layouts = catalog.layout_templates()
    if layouts:
        print("\nText layouts:")
        for entry in layouts:
            print(f"  {entry.id:<28} {entry.name}")

    if catalog.broken:
        print("\nTemplates that failed to load:", file=sys.stderr)
        for broken in catalog.broken:
            print(f"  {broken.id}: {broken.message}", file=sys.stderr)
    return EXIT_OK


def _run_check(args: argparse.Namespace, service: LabelSheetService) -> int:
    template, name = _resolve_template(service, args.template)
    report = validate_geometry(template)
    print(
        f"{name}: {len(template.elements)} element(s), fields: "
        f"{', '.join(template.field_names) or '(none)'}"
    )
    print(
        f"  label {report.label_width_mm:.2f} x {report.label_height_mm:.2f} mm, "
        f"{template.grid.cells_per_page} per sheet"
    )
    for issue in report.warnings:
        print(f"  warning: {issue.message}")
    for issue in report.errors:
        print(f"  error: {issue.message}", file=sys.stderr)
    if not report.ok:
        return EXIT_ERROR
    print("  ok")
    return EXIT_OK


def _run_avery(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if _exists(output) and not args.force:
        raise ValidationError(f"{output} already exists; pass --force to overwrite it")
    if output.suffix.lower() != ".json":
        raise ValidationError("the output path must end in .json")
    template = build_template(args.code, name=args.name)
    _write_output(output, dumps_json(template.dump()))
    print(f"wrote {output}")
    return EXIT_OK


def _run_import(args: argparse.Namespace) -> int:
    """Measure a label sheet PDF and write the template it implies.

    Everything the importer was unsure about goes to stderr, so a shell
    pipeline still gets a clean JSON file on stdout's sibling while a person
    sees the detection method, its confidence, and every caveat.
    """
    source = Path(args.source)
    output = Path(args.output)
    if _exists(output) and not args.force:
        raise ValidationError(f"{output} already exists; pass --force to overwrite it")
    if output.suffix.lower() != ".json":
        raise ValidationError("the output path must end in .json")
    if source.suffix.lower() != ".pdf":
        raise ValidationError("the source path must end in .pdf")
    if not _is_file(source):
        raise NotFoundError(f"no PDF file at {source}")

    pdf_bytes = read_bytes(source, max_bytes=MAX_PDF_BYTES, what=f"PDF {source.name}")
    report = import_template(
        pdf_bytes,
        name=args.name if args.name else source.stem,
        preset_code=args.template_code,
        rows=args.rows,
        cols=args.cols,
        label_width_mm=args.label_width_mm,
        label_height_mm=args.label_height_mm,
        margin_left_mm=args.margin_left_mm,
        margin_top_mm=args.margin_top_mm,
        gap_x_mm=args.gap_x_mm,
        gap_y_mm=args.gap_y_mm,
    )

    detected = report.detected
    if detected is None:
        print("no label grid was detected in the PDF", file=sys.stderr)
    else:
        print(
            f"detected a {detected.cols}x{detected.rows} grid from the page "
            f"{detected.method} (confidence {detected.confidence:.2f})",
            file=sys.stderr,
        )
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    _write_output(output, dumps_json(report.template.dump()))
    print(f"wrote {output}")
    return EXIT_OK


def _run_serve(args: argparse.Namespace) -> int:
    import os  # noqa: PLC0415

    if args.host:
        os.environ["LSG_HOST"] = args.host
    if args.port:
        os.environ["LSG_PORT"] = str(args.port)
    from label_sheet_generator.api.app import run  # noqa: PLC0415

    return run()


def _pick(override: float | None, current: float) -> float:
    return current if override is None else override


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Not configure(): a CLI should not emit JSON log records. This only turns
    # down third-party chatter so our own stderr messages are what the user sees.
    quiet_noisy_libraries()

    try:
        if args.command == "serve":
            return _run_serve(args)
        if args.command == "avery-template":
            return _run_avery(args)
        if args.command == "import-template":
            return _run_import(args)

        settings = _settings(args)
        service = LabelSheetService(settings, Catalog.build(settings))
        try:
            if args.command == "generate":
                return _run_generate(args, service)
            if args.command == "list-templates":
                return _run_list(args, service)
            if args.command == "check":
                return _run_check(args, service)
            # argparse has required=True and a fixed choice set, so this is
            # only reachable if a subcommand is added without a handler.
            parser.error(f"unsupported command: {args.command}")
        finally:
            service.close()
    except LabelSheetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, ValidationError | NotFoundError) else EXIT_ERROR
    except ConfigurationError as exc:  # pragma: no cover - startup only
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def list_presets() -> list[str]:
    """Preset codes, exposed for tests and shell completion."""
    return [preset.code for preset in iter_presets()]
