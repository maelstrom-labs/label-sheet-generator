"""The template catalogue: every available template, parsed once at startup.

Replaces the old ``presets`` module, which had three defects this file exists
to remove:

* It rebuilt the index on every request by walking the tree and fully parsing
  each file, so listing templates cost a directory walk plus N JSON parses.
  Here the walk happens once in :meth:`Catalog.build` and every lookup after
  that is a dictionary hit with no filesystem access at all.
* It wrapped the per-file parse in ``except (OSError, TemplateError, ValueError):
  continue``, so a template with one bad key simply vanished from the list with
  no way to find out why. A file that fails to parse is now a
  :class:`BrokenEntry` carrying the reason.
* It resolved directories relative to the process working directory. Roots now
  come from :class:`~label_sheet_generator.settings.Settings`, and every path
  goes through :mod:`label_sheet_generator.fsio`.

The format-sniffing loader lives here too, because the catalogue is its only
real consumer: templates on disk predate the explicit ``template_type``
discriminator, and existing user files have to keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError as PydanticValidationError

from label_sheet_generator import avery, fsio, geometry
from label_sheet_generator.errors import LabelSheetError, NotFoundError, TemplateError
from label_sheet_generator.schema import LabelTemplate, TemplateDefinition, TextLayoutTemplate
from label_sheet_generator.settings import Settings
from label_sheet_generator.units import quantize

__all__ = [
    "BrokenEntry",
    "Catalog",
    "CatalogEntry",
    "parse_template_document",
]

EntryKind = Literal["label", "text-layout"]
EntrySource = Literal["builtin", "user", "preset"]

#: The only template file extension the catalogue indexes.
_TEMPLATE_SUFFIX = ".json"

#: Id prefix marking a built-in stock preset rather than a file on disk.
_PRESET_PREFIX = "avery:"

#: Decimal places kept in the geometry block. The frontend draws thumbnails at
#: roughly one pixel per millimetre, so 4dp is far finer than anything visible,
#: while still round-tripping an exact inch fraction such as 66.675mm.
_GEOMETRY_PRECISION = 4

#: How much of a caller-supplied id is echoed back in a "not found" message.
#: Enough to identify a typo, short enough that a large hostile id cannot be
#: reflected into logs or a response body.
_MAX_ID_ECHO = 120


# --------------------------------------------------------------------------
# Format sniffing
# --------------------------------------------------------------------------


def parse_template_document(data: dict[str, Any], *, what: str = "template") -> TemplateDefinition:
    """Parse a template document of any accepted shape.

    Four shapes are accepted, because all four exist in the wild:

    * an explicit ``"template_type": "label"`` or ``"text-layout"``;
    * no ``template_type`` but ``page`` or ``grid`` present, which is a label
      sheet (this is what every pre-discriminator file on disk looks like);
    * no ``template_type`` and only ``elements``, which is a text layout;
    * ``*_mm`` or ``*_in`` lengths in any of the above, which
      :mod:`label_sheet_generator.schema` resolves.

    Raises:
        TemplateError: for any other shape, and for any schema violation. A
            pydantic ``ValidationError`` never escapes: it is converted here,
            with the field-level problems preserved on ``.details``.
    """
    if not isinstance(data, dict):
        raise TemplateError(f"{what} must be a JSON object, not {type(data).__name__}")

    declared = data.get("template_type")
    if declared is not None:
        if not isinstance(declared, str):
            raise TemplateError(
                f"{what} has a non-string template_type; expected 'label' or 'text-layout'"
            )
        if declared == "label":
            model: type[LabelTemplate] | type[TextLayoutTemplate] = LabelTemplate
        elif declared == "text-layout":
            model = TextLayoutTemplate
        else:
            raise TemplateError(
                f"{what} declares template_type {declared!r}; expected 'label' or 'text-layout'"
            )
    elif "page" in data or "grid" in data:
        model = LabelTemplate
    elif "elements" in data:
        model = TextLayoutTemplate
    else:
        raise TemplateError(
            f"{what} is not a recognised template: it needs a template_type, or a "
            "page and grid for a label sheet, or elements for a text layout"
        )

    try:
        return model.model_validate(data)
    except PydanticValidationError as exc:
        kind = "label" if model is LabelTemplate else "text-layout"
        raise TemplateError(
            f"{what} is not a valid {kind} template: {_summarize(exc)}",
            details=_error_details(exc),
        ) from exc
    except (TypeError, ValueError) as exc:
        # Defensive: a validator that raises something pydantic does not wrap
        # would otherwise leave this function as an HTTP 500.
        raise TemplateError(f"{what} could not be parsed: {exc}") from exc


def _error_details(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Flatten pydantic's errors into JSON-safe ``{loc, msg, type}`` records."""
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        details.append(
            {
                "loc": [part for part in error.get("loc", ()) if isinstance(part, (str, int))],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return details


def _summarize(exc: PydanticValidationError) -> str:
    """One-line summary of a pydantic failure for the human-facing message."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "the document did not validate"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "document"
    suffix = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{location}: {first.get('msg', 'invalid')}{suffix}"


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One usable template, already parsed and geometry-checked."""

    id: str
    kind: EntryKind
    name: str
    source: EntrySource
    path: Path | None
    template: TemplateDefinition
    fields: tuple[str, ...]
    geometry: dict[str, Any] | None
    labels_per_page: int
    warnings: tuple[str, ...] = ()

    @property
    def description(self) -> str | None:
        """Human-facing blurb from the template metadata, if it has one."""
        metadata = self.template.metadata
        for key in ("preset_description", "description"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def aliases(self) -> tuple[str, ...]:
        """Alternative codes this entry also answers to."""
        value = self.template.metadata.get("preset_aliases")
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return ()

    def to_dict(self) -> dict[str, Any]:
        """The catalogue listing shape the API and frontend consume."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "fields": list(self.fields),
            "geometry": self.geometry,
            "labels_per_page": self.labels_per_page,
            "units": self.template.units,
            "description": self.description,
            "aliases": list(self.aliases),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class BrokenEntry:
    """A template file that exists but could not be loaded.

    Reported rather than skipped: "my template disappeared from the list" with
    no further information was the single worst failure mode of the old index.
    """

    id: str
    path: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "message": self.message}


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


class Catalog:
    """An immutable, fully materialised index of every available template.

    Build it once with :meth:`build` and share it: lookups touch no files, so a
    request never pays for a directory walk or a JSON parse.
    """

    __slots__ = ("_broken", "_entries", "_preset_ids")

    def __init__(
        self,
        entries: dict[str, CatalogEntry],
        broken: list[BrokenEntry],
        preset_ids: frozenset[str],
    ) -> None:
        """Internal. Use :meth:`build`."""
        self._entries = entries
        self._broken = broken
        self._preset_ids = preset_ids

    @classmethod
    def build(cls, settings: Settings, *, include_presets: bool = True) -> Catalog:
        """Scan the configured roots and parse everything found.

        The built-in root is read first, then the user root when one is
        configured, so a user file with the same id shadows the built-in. The
        shadowing is recorded as a warning on the surviving entry rather than
        being silent, because a stale copy in a user directory masking a
        shipped template is confusing to debug.
        """
        entries: dict[str, CatalogEntry] = {}
        broken: list[BrokenEntry] = []

        roots: list[tuple[Path, EntrySource]] = [(settings.builtin_template_root, "builtin")]
        if settings.user_template_root is not None:
            roots.append((settings.user_template_root, "user"))

        for root, source in roots:
            _scan_root(root, source, settings, entries, broken)

        preset_ids: set[str] = set()
        if include_presets:
            for preset in avery.iter_presets():
                entry = _preset_entry(preset)
                entries[entry.id] = entry
                preset_ids.add(entry.id)

        return cls(entries, broken, frozenset(preset_ids))

    def get(self, entry_id: str) -> CatalogEntry:
        """Look up one entry by id.

        Preset ids such as ``avery:5160`` are resolved through
        :mod:`label_sheet_generator.avery` first, so an alias code works and no
        path-shaped handling ever sees them -- which matters because
        ``avery:5160`` deliberately does not match
        :data:`~label_sheet_generator.fsio.SAFE_ID_RE`.

        Raises:
            NotFoundError: if no entry has that id.
        """
        candidate = str(entry_id).strip()

        if candidate.lower().startswith(_PRESET_PREFIX):
            preset = avery.get_preset(candidate[len(_PRESET_PREFIX) :])
            resolved = _preset_id(preset.code)
            entry = self._entries.get(resolved)
            if entry is None:
                raise NotFoundError(f"preset {preset.code} is not in this catalog")
            return entry

        entry = self._entries.get(candidate)
        if entry is None:
            raise NotFoundError(f"template {_clip(candidate)!r} was not found")
        return entry

    def label_templates(self) -> list[CatalogEntry]:
        """Every full sheet definition, built-ins first, presets last."""
        return [entry for entry in self._entries.values() if entry.kind == "label"]

    def layout_templates(self) -> list[CatalogEntry]:
        """Every elements-only layout."""
        return [entry for entry in self._entries.values() if entry.kind == "text-layout"]

    @property
    def broken(self) -> list[BrokenEntry]:
        """Files that failed to load, with the reason for each."""
        return list(self._broken)

    @property
    def preset_ids(self) -> frozenset[str]:
        """Ids of the entries that came from built-in stock presets."""
        return self._preset_ids

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, entry_id: object) -> bool:
        return isinstance(entry_id, str) and entry_id in self._entries


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def _scan_root(
    root: Path,
    source: EntrySource,
    settings: Settings,
    entries: dict[str, CatalogEntry],
    broken: list[BrokenEntry],
) -> None:
    """Index one root in place, appending anything unreadable to ``broken``."""
    for entry_id in fsio.iter_ids(root, suffix=_TEMPLATE_SUFFIX):
        relative = f"{entry_id}{_TEMPLATE_SUFFIX}"
        try:
            path = fsio.resolve_in_root(root, entry_id, suffix=_TEMPLATE_SUFFIX, kind="template")
            data = fsio.read_json(
                path, max_bytes=settings.max_template_bytes, what=f"template {relative}"
            )
            template = parse_template_document(data, what=f"template {relative}")
        except LabelSheetError as exc:
            broken.append(BrokenEntry(id=entry_id, path=relative, message=exc.message))
            continue
        except (OSError, ValueError) as exc:
            # fsio converts these already; this is the belt-and-braces layer
            # that keeps one unreadable file from aborting the whole scan.
            broken.append(BrokenEntry(id=entry_id, path=relative, message=str(exc)))
            continue

        warnings: list[str] = []
        shadowed = entries.get(entry_id)
        if shadowed is not None:
            warnings.append(
                f"this {source} template shadows the {shadowed.source} template with the same id"
            )

        entries[entry_id] = _make_entry(
            entry_id=entry_id,
            source=source,
            path=path,
            template=template,
            warnings=warnings,
        )


def _make_entry(
    *,
    entry_id: str,
    source: EntrySource,
    path: Path | None,
    template: TemplateDefinition,
    warnings: list[str],
    name: str | None = None,
) -> CatalogEntry:
    """Assemble one entry, running the geometry check for label sheets."""
    all_warnings = list(warnings)
    geometry_block: dict[str, Any] | None = None
    labels_per_page = 0
    kind: EntryKind = "text-layout"

    if isinstance(template, LabelTemplate):
        kind = "label"
        labels_per_page = template.grid.cells_per_page
        geometry_block, geometry_messages = _geometry_block(template)
        all_warnings.extend(geometry_messages)

    return CatalogEntry(
        id=entry_id,
        kind=kind,
        name=name or template.name or entry_id,
        source=source,
        path=path,
        template=template,
        fields=tuple(template.field_names),
        geometry=geometry_block,
        labels_per_page=labels_per_page,
        warnings=tuple(all_warnings),
    )


def _geometry_block(template: LabelTemplate) -> tuple[dict[str, Any] | None, list[str]]:
    """The block the frontend draws an SVG thumbnail from, plus any complaints.

    Geometry *errors* are surfaced alongside the warnings: the file parsed, so
    the entry is listed, but a sheet whose grid runs off the page must not be
    presented as if it were fine.
    """
    try:
        report = geometry.validate(template)
    except LabelSheetError as exc:  # pragma: no cover - validate reports, never raises
        return None, [exc.message]

    grid = template.grid
    block: dict[str, Any] = {
        "page_width_mm": _round(template.page.width_mm),
        "page_height_mm": _round(template.page.height_mm),
        "rows": grid.rows,
        "cols": grid.cols,
        # Taken from the report because it derives them with
        # geometry.resolve_label_size_mm, which fills in a size the template
        # left implicit.
        "label_width_mm": _round(report.label_width_mm),
        "label_height_mm": _round(report.label_height_mm),
        "gap_x_mm": _round(grid.gap_x_mm),
        "gap_y_mm": _round(grid.gap_y_mm),
        "margin_top_mm": _round(grid.margin_top_mm),
        "margin_right_mm": _round(grid.margin_right_mm),
        "margin_bottom_mm": _round(grid.margin_bottom_mm),
        "margin_left_mm": _round(grid.margin_left_mm),
    }

    messages = [issue.message for issue in report.errors]
    messages.extend(issue.message for issue in report.warnings)
    return block, messages


def _preset_entry(preset: avery.AveryPreset) -> CatalogEntry:
    """Turn a stock preset into a catalogue entry. Never touches the disk."""
    return _make_entry(
        entry_id=_preset_id(preset.code),
        source="preset",
        path=None,
        template=avery.build_template(preset.code),
        warnings=[],
        name=f"Avery {preset.code}",
    )


def _preset_id(code: str) -> str:
    return f"{_PRESET_PREFIX}{code.lower()}"


def _round(value: float) -> float:
    return quantize(value, _GEOMETRY_PRECISION)


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_ID_ECHO else text[:_MAX_ID_ECHO] + "..."
