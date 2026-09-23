"""Record documents: parsing, coercion, and validation. No I/O, no rendering.

A record document is the data half of a label sheet -- one mapping per label,
plus a declared column order:

    {"schema": ["name", "sku"], "records": [{"name": "Ada", "sku": "A-1"}]}

A bare top-level JSON array is accepted on input and normalised to that shape,
because every example shipped with the package is a bare array and user files
on disk must keep working.

Everything here is total over its input: any malformed document produces a
:class:`~label_sheet_generator.errors.RecordError` (or ``LimitExceeded``), never
a ``TypeError``, ``KeyError``, ``UnicodeDecodeError`` or ``_csv.Error``. That is
the whole point of the module -- this is the layer an untrusted upload hits
first, and an untyped exception here is an HTTP 500.

Four defects in the previous implementation are fixed by construction:

* **Values reached the page as Python reprs.** A nested object was passed
  through ``str()``, so ``{'a': 1}`` was printed on a label. Nested values are
  now rejected, naming the row and the field.
* **CSV import was lossy and crashy.** Comma was assumed, ragged rows raised or
  silently dropped columns, duplicate headers overwrote each other, and a
  malformed quote escaped as ``_csv.Error``. Import now sniffs the delimiter,
  pads short rows, reports long ones, de-duplicates headers, and reports every
  loss as a warning rather than discarding data silently.
* **Upload format was chosen from the filename.** The extension is
  attacker-supplied; the content is not. Format is sniffed from the bytes, and
  the extension only breaks a tie.
* **No limits.** A 500MB paste or a single 200MB CSV field was accepted.

Row numbers in messages and warnings are 1-based and match what the user sees:
for JSON that is the position in the ``records`` array, for delimited input it
is the physical file row, so the header is row 1 and the first record is row 2.

Warning codes emitted (all non-fatal, all preserving data):
``invalid_field_name``, ``empty_field_name``, ``duplicate_field_name``,
``blank_header``, ``duplicate_header``, ``row_overflow``, ``row_underflow``,
``unknown_key``, ``fallback_encoding``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from label_sheet_generator.errors import LimitExceeded, RecordError, ValidationError
from label_sheet_generator.fsio import loads_json
from label_sheet_generator.schema import FIELD_NAME_RE
from label_sheet_generator.units import check_finite

#: Bytes of a delimited document handed to :class:`csv.Sniffer`. The sniffer is
#: O(n) in the sample and gains nothing from more data; 8KB covers well over a
#: hundred typical rows, which is far more than it needs to decide.
SNIFF_SAMPLE_BYTES = 8192

#: Delimiters offered to the sniffer, in preference order. Restricting the set
#: matters: left to its own devices the sniffer will happily conclude that a
#: letter appearing in every line is the delimiter and shred the document.
CANDIDATE_DELIMITERS = ",\t;|"

#: Used when the sniffer cannot decide -- e.g. a genuine single-column file.
DEFAULT_DELIMITER = ","

#: Per-field cap for the csv module. The default is 128KB but it is a *process
#: global* that any library can raise, so it is set explicitly rather than
#: trusted. One megabyte is far beyond any real label field and bounds the
#: allocation a single unterminated quote can provoke.
CSV_FIELD_SIZE_LIMIT = 1_000_000

#: Tried first for uploads; strips a UTF-8 BOM as a side effect.
PRIMARY_ENCODING = "utf-8-sig"

#: Windows spreadsheet exports that are not UTF-8 are almost always this.
#: Chosen over latin-1 because latin-1 decodes every byte sequence, which would
#: turn "this is not text" into mojibake instead of an error.
FALLBACK_ENCODING = "cp1252"

#: Floats at or above 2**53 have no exact integer successor, so ``is_integer``
#: stops implying the value round-trips; above this magnitude the plain float
#: repr is the honest rendering.
MAX_EXACT_INTEGER_FLOAT = 2.0**53

#: Name given to a column whose header cell is blank. The column is kept rather
#: than dropped -- silently losing a column of user data is worse than an odd
#: name they can rename.
BLANK_HEADER_PREFIX = "column_"

#: File extensions that break a tie when the content sniff is inconclusive.
_JSON_SUFFIXES = frozenset({".json"})
_DELIMITED_SUFFIXES = frozenset({".csv", ".tsv", ".tab", ".txt", ".psv"})

#: A document whose first non-space character is one of these is JSON.
_JSON_OPENERS = ("[", "{")

#: Row numbers in delimited warnings are physical file rows, so the header is
#: row 1 and the first data row is row 2 -- the numbering the user sees in
#: their spreadsheet.
HEADER_ROW_NUMBER = 1


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordDocument:
    """A validated record set: a column order plus rows of string values."""

    schema: tuple[str, ...]
    records: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        """The on-disk document shape, with record keys in schema order."""
        return {
            "schema": list(self.schema),
            "records": [_ordered_record(record, self.schema) for record in self.records],
        }

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class ParseReport:
    """A parsed document plus what was detected and what was lost on the way."""

    document: RecordDocument
    detected: dict[str, str]
    warnings: list[dict[str, Any]]


#: What an untouched editor pane parses to. Shared because it is immutable.
EMPTY_DOCUMENT = RecordDocument(schema=(), records=())


# --------------------------------------------------------------------------
# Field names and value coercion
# --------------------------------------------------------------------------


def _warn(
    warnings: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    row: int | None = None,
) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    if row is not None:
        entry["row"] = row
    warnings.append(entry)


def _check_field_name(
    name: str,
    warnings: list[dict[str, Any]],
    *,
    row: int | None = None,
    seen_invalid: set[str],
) -> None:
    """Warn once per offending name; never reject.

    A field a template cannot reference is still data the user typed, and
    dropping their column or refusing their file teaches them nothing. The
    renderer simply never reads it.
    """
    if FIELD_NAME_RE.match(name) or name in seen_invalid:
        return
    seen_invalid.add(name)
    _warn(
        warnings,
        "invalid_field_name",
        f"field {name!r} cannot be referenced from a template; template fields must "
        "start with a letter or underscore and contain only letters, digits and underscores",
        row=row,
    )


def _coerce_value(value: Any, *, row: int, field: str, max_value_length: int | None) -> str:
    """Convert one record value to the string that will be printed.

    Raises:
        RecordError: for nested or over-long values, naming the row and field.
    """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        # Lowercase so a round-trip through the JSON editor still reads as JSON.
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = _format_float(value, row=row, field=field)
    elif isinstance(value, (Mapping, list, tuple, set, frozenset, bytes, bytearray)):
        raise RecordError(
            f"record {row} field {field!r} is a nested {type(value).__name__}; "
            "record values must be text, numbers, booleans or null",
            loc=("records", row - 1, field),
        )
    else:
        raise RecordError(
            f"record {row} field {field!r} has unsupported type {type(value).__name__}; "
            "record values must be text, numbers, booleans or null",
            loc=("records", row - 1, field),
        )

    if max_value_length is not None and len(text) > max_value_length:
        raise RecordError(
            f"record {row} field {field!r} is {len(text)} characters; "
            f"the maximum is {max_value_length}",
            loc=("records", row - 1, field),
        )
    return text


def _format_float(value: float, *, row: int, field: str) -> str:
    """Render a float as a label would show it: no gratuitous trailing ``.0``."""
    try:
        check_finite(value)
    except ValueError as exc:
        raise RecordError(
            f"record {row} field {field!r} is not a finite number",
            loc=("records", row - 1, field),
        ) from exc
    if value.is_integer() and abs(value) < MAX_EXACT_INTEGER_FLOAT:
        return str(int(value))
    return repr(value)


def _ordered_record(record: Mapping[str, str], schema: Sequence[str]) -> dict[str, str]:
    """Schema columns first, then anything else, so diffs stay stable."""
    ordered = {name: record[name] for name in schema if name in record}
    for key, value in record.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def build_schema(
    fields: Sequence[str],
    *,
    schema: Sequence[Any] | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Merge template fields, a declared schema, and observed record keys.

    Order is template fields, then the declared schema, then keys discovered in
    the records. That ordering is what makes the generated JSON document open
    with the columns the selected template actually prints.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def remember(raw: Any) -> None:
        name = raw if isinstance(raw, str) else str(raw)
        name = name.strip()
        if not name or name in seen:
            return
        seen.add(name)
        ordered.append(name)

    for field_name in fields:
        remember(field_name)
    for field_name in schema or []:
        remember(field_name)
    for record in records or []:
        if isinstance(record, Mapping):
            for field_name in record:
                remember(field_name)

    return ordered


def analyze(fields: Sequence[str], document: RecordDocument) -> dict[str, Any]:
    """Compare what a template needs against what a document provides."""
    available = set(document.schema)
    wanted = list(dict.fromkeys(fields))
    return {
        "missing_fields": [name for name in wanted if name not in available],
        "extra_fields": [name for name in document.schema if name not in wanted],
        "record_count": len(document),
    }


# --------------------------------------------------------------------------
# JSON documents
# --------------------------------------------------------------------------


def _load_json(text: str) -> Any:
    """Parse JSON, reporting a syntax error's line and column.

    Goes through :func:`fsio.loads_json` so the ``NaN``/``Infinity`` literals
    are refused here exactly as they are for templates.
    """
    try:
        return loads_json(text, what="record document")
    except ValidationError as exc:
        cause = exc.__cause__
        if isinstance(cause, json.JSONDecodeError):
            raise RecordError(
                f"record document is not valid JSON: {cause.msg} "
                f"(line {cause.lineno}, column {cause.colno})",
                loc=("records",),
            ) from exc
        raise RecordError(str(exc), loc=("records",)) from exc


def _split_document(payload: Any) -> tuple[list[Any], list[Any], list[str]]:
    """Return ``(declared_schema, raw_records, unknown_top_level_keys)``."""
    if isinstance(payload, list):
        return [], payload, []

    if not isinstance(payload, Mapping):
        raise RecordError(
            "record document must be a JSON array of records, or an object with a 'records' array",
            loc=("records",),
        )

    if "records" not in payload:
        raise RecordError(
            "record document object is missing its 'records' array; expected "
            '{"schema": [...], "records": [...]} or a bare array of records',
            loc=("records",),
        )

    raw_records = payload["records"]
    if not isinstance(raw_records, list):
        raise RecordError("record document 'records' must be an array", loc=("records",))

    declared = payload.get("schema")
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        raise RecordError(
            "record document 'schema' must be an array of field names when present",
            loc=("schema",),
        )

    unknown = [str(key) for key in payload if key not in ("schema", "records")]
    return declared, raw_records, unknown


def _normalize_records(
    raw_records: Sequence[Any],
    warnings: list[dict[str, Any]],
    *,
    max_value_length: int | None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_invalid: set[str] = set()

    for offset, raw_record in enumerate(raw_records):
        row = offset + 1
        if not isinstance(raw_record, Mapping):
            raise RecordError(
                f"record {row} must be an object mapping field names to values, "
                f"not a {type(raw_record).__name__}",
                loc=("records", offset),
            )

        record: dict[str, str] = {}
        for raw_key, raw_value in raw_record.items():
            key = (raw_key if isinstance(raw_key, str) else str(raw_key)).strip()
            if not key:
                _warn(
                    warnings,
                    "empty_field_name",
                    f"record {row} has a field with a blank name; that column was dropped",
                    row=row,
                )
                continue
            if key in record:
                _warn(
                    warnings,
                    "duplicate_field_name",
                    f"record {row} names field {key!r} more than once; the last value was kept",
                    row=row,
                )
            _check_field_name(key, warnings, row=row, seen_invalid=seen_invalid)
            record[key] = _coerce_value(
                raw_value, row=row, field=key, max_value_length=max_value_length
            )
        records.append(record)

    return records


def _enforce_record_limit(count: int, max_records: int, *, exact: bool = True) -> None:
    """Raise if ``count`` rows is too many.

    ``exact`` is False while streaming a delimited file, where the parser stops
    at the first row past the cap and so does not know the true total.
    """
    if count <= max_records:
        return
    raise LimitExceeded(
        f"document contains {count} records; the maximum is {max_records}"
        if exact
        else f"document contains more than {max_records} records",
        limit_name="max_records",
        limit=max_records,
        actual=count if exact else None,
        loc=("records",),
    )


def _parse_json_document(
    text: str,
    *,
    max_records: int,
    max_value_length: int,
) -> tuple[RecordDocument, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if text.strip() == "":
        return EMPTY_DOCUMENT, warnings

    declared, raw_records, unknown = _split_document(_load_json(text))
    for key in unknown:
        _warn(
            warnings,
            "unknown_key",
            f"record document key {key!r} is not recognised and was ignored",
        )

    _enforce_record_limit(len(raw_records), max_records)
    records = _normalize_records(raw_records, warnings, max_value_length=max_value_length)

    seen_invalid: set[str] = set()
    for name in build_schema([], schema=declared):
        _check_field_name(name, warnings, seen_invalid=seen_invalid)

    schema = build_schema([], schema=declared, records=records)
    return RecordDocument(schema=tuple(schema), records=tuple(records)), warnings


def parse_document(text: str, *, max_records: int, max_value_length: int) -> RecordDocument:
    """Parse a JSON record document, in either the object or bare-array form.

    Args:
        text: The document source. Empty or whitespace-only yields an empty
            document, which is what an untouched editor pane contains.
        max_records: Row cap; exceeding it raises ``LimitExceeded``.
        max_value_length: Per-value character cap.

    Raises:
        RecordError: on a syntax error (reported with line and column), a
            non-object record, a nested value, or an over-long value.
        LimitExceeded: if the document declares more than ``max_records`` rows.
    """
    document, _ = _parse_json_document(
        text, max_records=max_records, max_value_length=max_value_length
    )
    return document


def format_document(
    fields: Sequence[str],
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    schema: Sequence[Any] | None = None,
) -> str:
    """Render a record document as the indented JSON the editor pane shows.

    Stable key order and two-space indentation are deliberate: this text is
    diffed, pasted into review comments, and edited by hand.

    Raises:
        RecordError: if a supplied record is not a mapping or holds a nested
            value.
    """
    merged_schema = build_schema(fields, schema=schema, records=records)
    # No length cap here: this path formats data the caller already holds,
    # and truncating someone's document on the way to the screen would be
    # worse than showing it.
    normalized = _normalize_records(list(records or []), [], max_value_length=None)
    payload = {
        "schema": merged_schema,
        "records": [_ordered_record(record, merged_schema) for record in normalized],
    }
    # ensure_ascii=False so accented names stay readable in the editor.
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)


# --------------------------------------------------------------------------
# Delimited documents
# --------------------------------------------------------------------------


def _sniff_delimiter(text: str) -> str:
    sample = text[:SNIFF_SAMPLE_BYTES]
    try:
        return csv.Sniffer().sniff(sample, delimiters=CANDIDATE_DELIMITERS).delimiter
    except csv.Error:
        # A single-column file has no delimiter to find; comma is harmless there.
        return DEFAULT_DELIMITER


def _build_header(
    raw_header: Sequence[str],
    warnings: list[dict[str, Any]],
) -> list[str]:
    """Turn the first row into unique, non-empty column names."""
    header: list[str] = []
    used: set[str] = set()

    for position, raw_cell in enumerate(raw_header, start=1):
        name = (raw_cell or "").strip().lstrip("\ufeff").strip()
        if not name:
            name = f"{BLANK_HEADER_PREFIX}{position}"
            _warn(
                warnings,
                "blank_header",
                f"column {position} has no header; it was named {name!r} so its data is kept",
                row=HEADER_ROW_NUMBER,
            )
        if name in used:
            # Suffix rather than overwrite: two columns called "name" hold two
            # columns of real data, and a dict would keep only the last.
            ordinal = 2
            while f"{name}_{ordinal}" in used:
                ordinal += 1
            renamed = f"{name}_{ordinal}"
            _warn(
                warnings,
                "duplicate_header",
                f"column {position} repeats the header {name!r}; it was renamed to "
                f"{renamed!r} so neither column is lost",
                row=HEADER_ROW_NUMBER,
            )
            name = renamed
        used.add(name)
        header.append(name)

    return header


def parse_delimited(text: str, *, max_records: int, max_value_length: int) -> ParseReport:
    """Parse CSV/TSV-style text with a header row into a record document.

    The delimiter is sniffed rather than assumed. Ragged rows are repaired, not
    rejected: a short row is padded with empty strings and a long row keeps its
    header-covered cells while the surplus is reported as a warning. Fully
    blank rows are skipped, since spreadsheet exports are full of them.

    Raises:
        RecordError: if there is no header row, the first row is empty, or the
            file is malformed (an unterminated quote, or a field over
            :data:`CSV_FIELD_SIZE_LIMIT`).
        LimitExceeded: if more than ``max_records`` data rows are present.
    """
    body = text.lstrip("\ufeff")
    if body.strip() == "":
        raise RecordError("delimited data is empty; a header row is required", loc=("records",))

    delimiter = _sniff_delimiter(body)
    warnings: list[dict[str, Any]] = []
    records: list[dict[str, str]] = []
    header: list[str] = []
    seen_invalid: set[str] = set()

    previous_limit = csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    try:
        # newline="" keeps CRLF inside quoted fields intact, per the csv docs.
        reader = csv.reader(io.StringIO(body, newline=""), delimiter=delimiter, strict=True)
        try:
            for raw_row in reader:
                if not header:
                    if not any((cell or "").strip() for cell in raw_row):
                        raise RecordError(
                            "the first row of the delimited data is blank; it must be a "
                            "header row naming the columns",
                            loc=("records", 0),
                        )
                    header = _build_header(raw_row, warnings)
                    for name in header:
                        _check_field_name(
                            name, warnings, row=HEADER_ROW_NUMBER, seen_invalid=seen_invalid
                        )
                    continue

                row_number = reader.line_num
                if not any((cell or "").strip() for cell in raw_row):
                    continue

                if len(raw_row) > len(header):
                    surplus = len(raw_row) - len(header)
                    _warn(
                        warnings,
                        "row_overflow",
                        f"row {row_number} has {surplus} value(s) more than the "
                        f"{len(header)} header columns; the surplus was discarded",
                        row=row_number,
                    )
                elif len(raw_row) < len(header):
                    _warn(
                        warnings,
                        "row_underflow",
                        f"row {row_number} has only {len(raw_row)} of {len(header)} "
                        "columns; the rest were filled with empty values",
                        row=row_number,
                    )

                record: dict[str, str] = {}
                for index, name in enumerate(header):
                    cell = raw_row[index] if index < len(raw_row) else ""
                    record[name] = _coerce_value(
                        cell,
                        row=row_number,
                        field=name,
                        max_value_length=max_value_length,
                    )
                records.append(record)
                _enforce_record_limit(len(records), max_records, exact=False)
        except csv.Error as exc:
            raise RecordError(
                f"delimited data is malformed near row {reader.line_num}: {exc}",
                loc=("records", max(reader.line_num - 1, 0)),
            ) from exc
    finally:
        csv.field_size_limit(previous_limit)

    if not header:
        raise RecordError("delimited data requires a header row", loc=("records",))

    schema = build_schema(header, records=records)
    return ParseReport(
        document=RecordDocument(schema=tuple(schema), records=tuple(records)),
        detected={"format": "delimited", "encoding": "", "delimiter": delimiter},
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


def _decode_upload(data: bytes, warnings: list[dict[str, Any]]) -> tuple[str, str]:
    """Decode upload bytes, returning ``(text, encoding_used)``."""
    if b"\x00" in data:
        raise RecordError(
            "uploaded file is not text; if it came from a spreadsheet, export it as "
            "UTF-8 CSV rather than UTF-16, XLSX or PDF",
            loc=("file",),
        )
    try:
        return data.decode(PRIMARY_ENCODING), PRIMARY_ENCODING
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode(FALLBACK_ENCODING)
    except UnicodeDecodeError as exc:
        raise RecordError(
            "uploaded file is not valid UTF-8 or Windows-1252 text; re-export it as UTF-8",
            loc=("file",),
        ) from exc
    _warn(
        warnings,
        "fallback_encoding",
        f"file was not valid UTF-8; it was decoded as {FALLBACK_ENCODING} and "
        "accented characters may be wrong",
    )
    return text, FALLBACK_ENCODING


def _suffix_of(filename: str | None) -> str:
    """Lowercase extension, taken from the basename only.

    Deliberately string work rather than ``Path``: the filename comes from a
    multipart header and may contain separators or drive letters, and this
    value is never used to touch the filesystem.
    """
    if not filename:
        return ""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def parse_upload(
    data: bytes,
    filename: str | None,
    *,
    fields: Sequence[str],
    max_records: int,
    max_value_length: int,
) -> ParseReport:
    """Parse an uploaded record file, choosing the format from its content.

    The filename is advisory only. It arrives from the client, so trusting its
    extension lets the caller pick the parser; instead the leading bytes decide
    (``[`` or ``{`` means JSON), and the extension is consulted only to break a
    tie when the content-based guess fails to parse.

    ``fields`` orders the resulting schema so the columns the chosen template
    prints come first.

    Raises:
        RecordError: on an undecodable, binary, empty, or malformed file.
        LimitExceeded: if the file holds more than ``max_records`` records.
    """
    warnings: list[dict[str, Any]] = []
    text, encoding = _decode_upload(data, warnings)

    stripped = text.lstrip().lstrip("\ufeff").lstrip()
    if stripped == "":
        raise RecordError("uploaded file is empty", loc=("file",))

    suffix = _suffix_of(filename)
    looks_json = stripped.startswith(_JSON_OPENERS)

    if looks_json:
        try:
            document, json_warnings = _parse_json_document(
                text, max_records=max_records, max_value_length=max_value_length
            )
        except RecordError:
            # A CSV whose first cell happens to start with a brace is the tie
            # the extension exists to break; a .json file keeps its syntax error.
            if suffix in _JSON_SUFFIXES or suffix not in _DELIMITED_SUFFIXES:
                raise
            report = parse_delimited(
                text, max_records=max_records, max_value_length=max_value_length
            )
            return _finalize_upload(report, fields, warnings, encoding)
        warnings.extend(json_warnings)
        report = ParseReport(
            document=document,
            detected={"format": "json", "encoding": encoding, "delimiter": ""},
            warnings=[],
        )
        return _finalize_upload(report, fields, warnings, encoding)

    report = parse_delimited(text, max_records=max_records, max_value_length=max_value_length)
    return _finalize_upload(report, fields, warnings, encoding)


def _finalize_upload(
    report: ParseReport,
    fields: Sequence[str],
    warnings: list[dict[str, Any]],
    encoding: str,
) -> ParseReport:
    """Re-order the schema around the template's fields and merge warnings."""
    document = report.document
    schema = build_schema(fields, schema=document.schema, records=document.records)
    detected = dict(report.detected)
    detected["encoding"] = encoding
    return ParseReport(
        document=RecordDocument(schema=tuple(schema), records=document.records),
        detected=detected,
        warnings=[*warnings, *report.warnings],
    )
