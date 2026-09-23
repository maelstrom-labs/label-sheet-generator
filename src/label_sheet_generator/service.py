"""The application service layer: the only thing the API and CLI call.

Composes the catalog, record parsing, geometry and rendering into the handful
of operations the product actually offers, and is where every *work* limit is
enforced -- record count, page count, output size, render concurrency and the
wall-clock budget.

Keeping this separate from :mod:`label_sheet_generator.api` is what lets the
CLI and the web app share one implementation without the CLI inheriting HTTP
concerns, and what keeps FastAPI out of the import graph of everything below.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from label_sheet_generator.assets import AssetLoader
from label_sheet_generator.catalog import Catalog, CatalogEntry
from label_sheet_generator.errors import (
    LimitExceeded,
    NotFoundError,
    Overloaded,
    RenderTimeout,
    TemplateError,
    ValidationError,
)
from label_sheet_generator.geometry import GeometryReport
from label_sheet_generator.geometry import validate as validate_geometry
from label_sheet_generator.records import RecordDocument, analyze, parse_document
from label_sheet_generator.render import RenderOptions, RenderResult, render_page_png, render_pdf
from label_sheet_generator.schema import LabelTemplate, TextLayoutTemplate
from label_sheet_generator.settings import Settings


@dataclass(frozen=True, slots=True)
class MarginOverrides:
    """Per-request margin overrides, in millimetres."""

    top_mm: float | None = None
    right_mm: float | None = None
    bottom_mm: float | None = None
    left_mm: float | None = None

    def is_empty(self) -> bool:
        return all(
            value is None for value in (self.top_mm, self.right_mm, self.bottom_mm, self.left_mm)
        )


@dataclass(frozen=True, slots=True)
class RenderPlan:
    """The cheap dry run: everything except the PDF itself."""

    template: LabelTemplate
    template_name: str
    layout_name: str
    fields: list[str]
    document: RecordDocument
    labels: int
    pages: int
    labels_per_page: int
    missing_fields: list[str]
    extra_fields: list[str]
    geometry: GeometryReport
    record_warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_name": self.template_name,
            "layout_name": self.layout_name,
            "fields": self.fields,
            "labels": self.labels,
            "pages": self.pages,
            "labels_per_page": self.labels_per_page,
            "missing_fields": self.missing_fields,
            "extra_fields": self.extra_fields,
            "geometry": self.geometry.to_dict(),
            "record_warnings": self.record_warnings,
        }


class LabelSheetService:
    """Stateless request handling over an immutable catalog.

    One instance is built at startup and shared. The only mutable state is the
    render semaphore and its thread pool, both of which exist to bound work
    rather than to carry per-request data.
    """

    def __init__(self, settings: Settings, catalog: Catalog) -> None:
        self.settings = settings
        self.catalog = catalog
        self.assets = AssetLoader(
            settings.asset_root,
            max_bytes=settings.max_asset_bytes,
            max_pixels=settings.max_asset_pixels,
        )
        # A pool separate from the ASGI threadpool, so a burst of renders cannot
        # starve cheap endpoints like /api/livez of a thread to run on.
        self._pool = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_renders,
            thread_name_prefix="lsg-render",
        )
        self._slots = threading.Semaphore(settings.max_concurrent_renders)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- Catalog ---------------------------------------------------------

    def bootstrap(self) -> dict[str, Any]:
        """Everything the frontend needs in one call."""
        return {
            "templates": [self._template_info(entry) for entry in self.catalog.label_templates()],
            "layouts": [entry.to_dict() for entry in self.catalog.layout_templates()],
            "broken": [entry.to_dict() for entry in self.catalog.broken],
            "limits": self.settings.public_limits(),
            "features": {
                "pdf_import": self.settings.enable_pdf_import,
                "assets": self.assets.enabled,
            },
        }

    def _template_info(self, entry: CatalogEntry) -> dict[str, Any]:
        """A catalog entry plus its shipped sample records, when it has any.

        Sending the sample with the catalog means a first-time visitor sees a
        real rendered sheet instead of an empty page, without a second
        round-trip or a client-side guess at what the fields mean.
        """
        payload = entry.to_dict()
        payload["example_document"] = self.example_document(entry.id)
        return payload

    def get_entry(self, entry_id: str) -> CatalogEntry:
        return self.catalog.get(entry_id)

    # -- Composition -----------------------------------------------------

    def resolve_template(
        self,
        template_id: str,
        layout_id: str | None = None,
        overrides: MarginOverrides | None = None,
    ) -> tuple[LabelTemplate, str, str]:
        """Return the effective template plus display names for it and the layout.

        A layout template contributes only its elements; the page and grid always
        come from the label template.
        """
        entry = self.catalog.get(template_id)
        if entry.kind != "label":
            raise ValidationError(
                f"{template_id!r} is a text layout, not a label template; "
                "select it in the layout field instead",
                loc=("template_id",),
            )
        template = entry.template
        if not isinstance(template, LabelTemplate):  # pragma: no cover - guarded above
            raise TemplateError(f"{template_id!r} is not a label template")

        layout_name = "Default"
        if layout_id:
            layout_entry = self.catalog.get(layout_id)
            if layout_entry.kind != "text-layout":
                raise ValidationError(
                    f"{layout_id!r} is a label template, not a text layout",
                    loc=("layout_id",),
                )
            layout = layout_entry.template
            if not isinstance(layout, TextLayoutTemplate):  # pragma: no cover
                raise TemplateError(f"{layout_id!r} is not a text layout")
            metadata = {**template.metadata, "layout_template": layout_entry.name}
            template = template.model_copy(
                update={"elements": list(layout.elements), "metadata": metadata}
            )
            layout_name = layout_entry.name

        if overrides is not None and not overrides.is_empty():
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

        return template, entry.name, layout_name

    def plan(
        self,
        *,
        template_id: str,
        layout_id: str | None,
        document: str,
        overrides: MarginOverrides | None = None,
    ) -> RenderPlan:
        """Validate everything and compute counts without producing a PDF.

        This is what the UI calls on every edit, so it must be cheap and must
        never raise for ordinary bad input -- geometry problems come back inside
        the report rather than as exceptions.
        """
        template, template_name, layout_name = self.resolve_template(
            template_id, layout_id, overrides
        )
        fields = template.field_names
        record_document = parse_document(
            document,
            max_records=self.settings.max_records,
            max_value_length=self.settings.max_field_value_length,
        )
        report = validate_geometry(template)

        labels_per_page = 0
        if report.ok:
            labels_per_page = template.grid.cells_per_page

        record_count = len(record_document)
        effective = record_count if record_count else 1
        pages = -(-effective // labels_per_page) if labels_per_page else 0

        alignment = analyze(fields, record_document)
        return RenderPlan(
            template=template,
            template_name=template_name,
            layout_name=layout_name,
            fields=fields,
            document=record_document,
            labels=record_count,
            pages=pages,
            labels_per_page=labels_per_page,
            missing_fields=alignment["missing_fields"],
            extra_fields=alignment["extra_fields"],
            geometry=report,
            record_warnings=[],
        )

    # -- Rendering -------------------------------------------------------

    def render(
        self,
        plan: RenderPlan,
        options: RenderOptions,
        *,
        timeout_s: float | None = None,
    ) -> RenderResult:
        """Render a plan to PDF bytes inside the concurrency and time budget.

        The semaphore sheds load rather than queueing it: an unbounded queue on
        a free-tier container turns a traffic spike into an OOM kill, whereas a
        503 with Retry-After degrades honestly.
        """
        if not plan.geometry.ok:
            first = plan.geometry.errors[0]
            raise TemplateError(first.message, details=[first.to_dict()])
        if plan.pages > self.settings.max_pages:
            raise LimitExceeded(
                f"this would produce {plan.pages} pages; the maximum is {self.settings.max_pages}",
                limit_name="max_pages",
                limit=self.settings.max_pages,
                actual=plan.pages,
            )

        budget = self.settings.render_timeout_s if timeout_s is None else timeout_s
        if not self._slots.acquire(timeout=1.0):
            raise Overloaded(retry_after=max(1, int(budget)))

        try:
            future = self._pool.submit(
                render_pdf,
                plan.template,
                [dict(record) for record in plan.document.records],
                options=options,
                assets=self.assets,
                max_pages=self.settings.max_pages,
                max_output_bytes=self.settings.max_output_bytes,
            )
            started = time.monotonic()
            try:
                return future.result(timeout=budget)
            except TimeoutError as exc:
                future.cancel()
                raise RenderTimeout(
                    f"rendering exceeded the {budget:.0f}s budget; "
                    "reduce the record count and try again"
                ) from exc
            finally:
                del started
        finally:
            self._slots.release()

    def preview_png(
        self,
        plan: RenderPlan,
        options: RenderOptions,
        *,
        page: int = 0,
        scale: float | None = None,
    ) -> bytes:
        """Render one page to PNG for the browser preview."""
        requested = self.settings.preview_scale_default if scale is None else scale
        clamped = min(
            max(requested, self.settings.preview_scale_min), self.settings.preview_scale_max
        )
        result = self.render(plan, options)
        return render_page_png(result.pdf_bytes, page=page, scale=clamped)

    # -- Examples --------------------------------------------------------

    def example_document(self, entry_id: str) -> str | None:
        """The shipped example records for a template, if there are any."""
        from label_sheet_generator.fsio import read_json  # noqa: PLC0415

        stem = entry_id.rsplit("/", maxsplit=1)[-1]
        path = self.settings.example_root / f"{stem}.json"
        if not path.is_file():
            return None
        try:
            payload = read_json(path, max_bytes=self.settings.max_document_bytes)
        except (NotFoundError, ValidationError, LimitExceeded):
            return None
        from label_sheet_generator.records import format_document  # noqa: PLC0415

        records = payload if isinstance(payload, list) else payload.get("records", [])
        entry = self.catalog.get(entry_id)
        return format_document(entry.fields, records=records)


def _pick(override: float | None, current: float) -> float:
    return current if override is None else override


def build_service(settings: Settings | None = None) -> LabelSheetService:
    """Construct a service with a freshly built catalog."""
    from label_sheet_generator.settings import default_settings  # noqa: PLC0415

    resolved = settings or default_settings()
    resolved.validate()
    return LabelSheetService(resolved, Catalog.build(resolved))


def paginate(count: int, per_page: int) -> int:
    """Pages needed for ``count`` labels, minimum one."""
    if per_page <= 0:
        return 0
    return max(1, -(-count // per_page))


def as_sequence(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in records]
