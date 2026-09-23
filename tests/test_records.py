"""Record documents: JSON parsing, value coercion, delimited import, uploads.

This is the layer untrusted input hits first, so the recurring assertion is
that a malformed document produces a typed RecordError or LimitExceeded and
never a builtin exception.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from label_sheet_generator.errors import LimitExceeded, RecordError
from label_sheet_generator.records import (
    RecordDocument,
    analyze,
    build_schema,
    format_document,
    parse_delimited,
    parse_document,
    parse_upload,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "src/label_sheet_generator/data/examples"

LIMITS: dict[str, int] = {"max_records": 1000, "max_value_length": 4096}


def parse(text: str, **overrides: int) -> RecordDocument:
    return parse_document(text, **{**LIMITS, **overrides})


def delimited(text: str, **overrides: int):
    return parse_delimited(text, **{**LIMITS, **overrides})


def upload(data: bytes, filename: str | None, fields: list[str] | None = None, **overrides: int):
    return parse_upload(data, filename, fields=fields or [], **{**LIMITS, **overrides})


def codes(report: Any) -> list[str]:
    return [warning["code"] for warning in report.warnings]


# --- JSON documents -------------------------------------------------------


def test_canonical_object_form_keeps_the_declared_schema_order() -> None:
    document = parse(
        json.dumps({"schema": ["sku", "name"], "records": [{"name": "Ada", "sku": "A-1"}]})
    )
    assert document.schema == ("sku", "name")
    assert document.records == ({"name": "Ada", "sku": "A-1"},)


def test_bare_top_level_array_is_accepted_and_its_schema_derived_from_the_keys() -> None:
    document = parse('[{"name": "Ada", "sku": "A-1"}, {"name": "Alan", "sku": "A-2"}]')
    assert document.schema == ("name", "sku")
    assert len(document) == 2


@pytest.mark.parametrize("example", ["basic-address.json", "spice-jar.json"])
def test_every_shipped_example_is_a_bare_array_that_parses(example: str) -> None:
    source = (EXAMPLES / example).read_text(encoding="utf-8")
    assert json.loads(source).__class__ is list
    document = parse(source)
    assert len(document) == 3
    assert "name" in document.schema


def test_keys_absent_from_the_declared_schema_are_appended_not_dropped() -> None:
    document = parse(
        json.dumps({"schema": ["name"], "records": [{"name": "Ada", "note": "extra"}]})
    )
    assert document.schema == ("name", "note")
    assert document.records[0]["note"] == "extra"


@pytest.mark.parametrize("source", ["", "   ", "\n\t "])
def test_blank_source_is_an_empty_document_not_an_error(source: str) -> None:
    document = parse(source)
    assert document.schema == ()
    assert len(document) == 0


def test_json_syntax_error_reports_the_line_and_column() -> None:
    with pytest.raises(RecordError) as caught:
        parse('[\n  {"name": "Ada"},\n  {"name" "Alan"}\n]')
    message = str(caught.value)
    assert "line 3" in message
    assert "column" in message


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_json_float_literals_are_refused(literal: str) -> None:
    # json.loads accepts these by default; a NaN reaching the geometry layer
    # defeats every bounds check, so the loader rejects them at the door.
    with pytest.raises(RecordError, match="JSON"):
        parse('[{"value": ' + literal + "}]")


@pytest.mark.parametrize(
    "source,expected",
    [
        ('[{"v": true}]', "true"),
        ('[{"v": false}]', "false"),
        ('[{"v": null}]', ""),
        ('[{"v": 7}]', "7"),
        ('[{"v": -7}]', "-7"),
        ('[{"v": 3.0}]', "3"),
        ('[{"v": -12.0}]', "-12"),
        ('[{"v": 1.5}]', "1.5"),
        ('[{"v": "already text"}]', "already text"),
    ],
)
def test_values_are_coerced_to_the_text_a_label_should_show(source: str, expected: str) -> None:
    assert parse(source).records[0]["v"] == expected


def test_an_integral_float_prints_without_a_trailing_point_zero() -> None:
    assert parse('[{"qty": 12.0}]').records[0]["qty"] == "12"


def test_a_float_too_large_for_exact_integers_keeps_its_float_form() -> None:
    # Above 2**53 is_integer() no longer implies the value round-trips, so the
    # plain repr is the honest rendering rather than a fabricated integer.
    assert "e" in parse('[{"v": 1e20}]').records[0]["v"]


@pytest.mark.parametrize(
    "source,type_name",
    [
        ('[{"name": "Ada"}, {"name": {"first": "Grace"}}]', "dict"),
        ('[{"name": "Ada"}, {"name": ["Grace", "Hopper"]}]', "list"),
    ],
)
def test_a_nested_value_is_rejected_naming_the_row_and_field(source: str, type_name: str) -> None:
    # The old code passed nested values through str(), so a Python repr such as
    # "{'first': 'Grace'}" was printed onto the label.
    with pytest.raises(RecordError) as caught:
        parse(source)
    message = caught.value.message
    assert "record 2" in message
    assert "'name'" in message
    assert type_name in message
    assert caught.value.loc == ("records", 1, "name")


def test_a_record_that_is_not_an_object_names_the_row() -> None:
    with pytest.raises(RecordError, match="record 2"):
        parse('[{"name": "Ada"}, "Grace"]')


@pytest.mark.parametrize(
    "source",
    [
        '"just a string"',
        "42",
        "true",
        '{"schema": ["name"]}',
        '{"records": {"name": "Ada"}}',
        '{"records": [], "schema": "name"}',
    ],
)
def test_structurally_wrong_documents_raise_a_typed_record_error(source: str) -> None:
    with pytest.raises(RecordError):
        parse(source)


def test_too_many_records_raises_limit_exceeded_with_the_limit_named() -> None:
    with pytest.raises(LimitExceeded) as caught:
        parse('[{"a": "1"}, {"a": "2"}, {"a": "3"}]', max_records=2)
    assert caught.value.limit_name == "max_records"
    assert caught.value.limit == 2
    assert caught.value.actual == 3


def test_an_over_long_value_names_the_row_and_field() -> None:
    with pytest.raises(RecordError) as caught:
        parse('[{"a": "ok"}, {"a": "far too long"}]', max_value_length=5)
    assert "record 2" in caught.value.message
    assert "'a'" in caught.value.message
    assert caught.value.loc == ("records", 1, "a")


def test_a_value_exactly_at_the_length_limit_is_accepted() -> None:
    assert parse('[{"a": "abcde"}]', max_value_length=5).records[0]["a"] == "abcde"


def test_an_unknown_top_level_key_is_a_warning_not_a_rejection() -> None:
    report = upload(b'{"records": [{"a": "1"}], "title": "ignored"}', "d.json")
    assert codes(report) == ["unknown_key"]
    assert len(report.document) == 1


def test_a_field_name_no_template_can_reference_warns_but_keeps_the_column() -> None:
    report = upload(b'[{"first name": "Ada"}]', "d.json")
    assert codes(report) == ["invalid_field_name"]
    assert report.document.records[0]["first name"] == "Ada"


# --- schema helpers -------------------------------------------------------


def test_build_schema_orders_template_fields_before_discovered_keys() -> None:
    assert build_schema(["name"], schema=["sku"], records=[{"note": "x", "name": "Ada"}]) == [
        "name",
        "sku",
        "note",
    ]


def test_analyze_reports_what_the_template_lacks_and_what_the_data_adds() -> None:
    document = parse('[{"name": "Ada", "note": "x"}]')
    assert analyze(["name", "sku"], document) == {
        "missing_fields": ["sku"],
        "extra_fields": ["note"],
        "record_count": 1,
    }


def test_analyze_reports_a_repeated_template_field_once() -> None:
    assert analyze(["sku", "sku"], parse("[]"))["missing_fields"] == ["sku"]


# --- format_document ------------------------------------------------------


def test_format_document_round_trips_through_parse_document() -> None:
    text = format_document(
        ["name", "sku"],
        records=[{"name": "Ada", "sku": "A-1"}, {"name": "Alan", "sku": "A-2"}],
    )
    document = parse(text)
    assert document.schema == ("name", "sku")
    assert document.records == ({"name": "Ada", "sku": "A-1"}, {"name": "Alan", "sku": "A-2"})
    assert format_document(list(document.schema), records=list(document.records)) == text


def test_format_document_emits_the_canonical_object_form() -> None:
    payload = json.loads(format_document(["name"], records=[{"name": "Ada"}]))
    assert payload == {"schema": ["name"], "records": [{"name": "Ada"}]}


def test_format_document_coerces_values_the_same_way_parsing_does() -> None:
    payload = json.loads(format_document(["v"], records=[{"v": 3.0}, {"v": True}, {"v": None}]))
    assert [record["v"] for record in payload["records"]] == ["3", "true", ""]


def test_format_document_refuses_a_nested_value_rather_than_writing_a_repr() -> None:
    with pytest.raises(RecordError, match="nested"):
        format_document(["v"], records=[{"v": {"nope": 1}}])


def test_format_document_puts_template_fields_first() -> None:
    payload = json.loads(format_document(["sku"], records=[{"name": "Ada", "sku": "A-1"}]))
    assert payload["schema"] == ["sku", "name"]
    assert list(payload["records"][0]) == ["sku", "name"]


# --- delimited import -----------------------------------------------------


@pytest.mark.parametrize("delimiter", [",", "\t", ";", "|"])
def test_the_delimiter_is_sniffed_rather_than_assumed(delimiter: str) -> None:
    rows = ["name{d}sku{d}city", "Ada{d}A-1{d}London", "Alan{d}A-2{d}Leeds"]
    report = delimited("\n".join(row.format(d=delimiter) for row in rows) + "\n")
    assert report.detected["delimiter"] == delimiter
    assert report.detected["format"] == "delimited"
    assert report.document.schema == ("name", "sku", "city")
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1", "city": "London"}


def test_a_utf8_byte_order_mark_is_not_part_of_the_first_header() -> None:
    report = delimited("﻿name,sku\nAda,A-1\n")
    assert report.document.schema == ("name", "sku")


def test_a_single_column_file_parses_without_a_delimiter_to_find() -> None:
    report = delimited("name\nAda\nGrace\n")
    assert report.document.schema == ("name",)
    assert len(report.document) == 2


@pytest.mark.parametrize("source", ["", "   \n\n", ",,\n1,2,3\n", '"",""\nAda,A-1\n'])
def test_delimited_data_without_a_usable_header_row_is_rejected(source: str) -> None:
    with pytest.raises(RecordError, match="header"):
        delimited(source)


def test_a_header_only_file_is_a_valid_empty_document() -> None:
    report = delimited("name,sku\n")
    assert report.document.schema == ("name", "sku")
    assert len(report.document) == 0


def test_duplicate_headers_are_suffixed_so_neither_column_is_lost() -> None:
    # A dict keyed on the raw header kept only the last of two columns called
    # "name", silently discarding a column of the user's data.
    report = delimited("name,sku,name\nAda,A-1,Lovelace\n")
    assert report.document.schema == ("name", "sku", "name_2")
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1", "name_2": "Lovelace"}
    assert codes(report) == ["duplicate_header"]
    assert report.warnings[0]["row"] == 1


def test_a_blank_header_cell_is_named_rather_than_dropping_its_column() -> None:
    report = delimited("name,,sku\nAda,keep me,A-1\n")
    assert report.document.schema == ("name", "column_2", "sku")
    assert report.document.records[0]["column_2"] == "keep me"
    assert codes(report) == ["blank_header"]


def test_a_short_row_is_padded_with_empty_values_and_reported() -> None:
    report = delimited("name,sku,city\nAda,A-1\n")
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1", "city": ""}
    assert codes(report) == ["row_underflow"]
    assert report.warnings[0]["row"] == 2


def test_a_long_row_keeps_its_header_columns_and_reports_the_surplus() -> None:
    report = delimited("name,sku\nAda,A-1,surplus,more\n")
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1"}
    assert codes(report) == ["row_overflow"]
    assert report.warnings[0]["row"] == 2


def test_ragged_rows_are_repaired_rather_than_crashing_the_import() -> None:
    report = delimited("name,sku\nAda\nGrace,G-1,extra\nAlan,A-2\n")
    assert len(report.document) == 3
    assert codes(report) == ["row_underflow", "row_overflow"]


def test_fully_blank_rows_are_skipped_without_a_warning() -> None:
    report = delimited("name,sku\nAda,A-1\n\n  ,  \n,\nGrace,G-1\n")
    assert len(report.document) == 2
    assert report.warnings == []


def test_warning_row_numbers_are_physical_file_rows_so_the_header_is_row_one() -> None:
    report = delimited("name,sku\nAda,A-1\n\nGrace\n")
    assert report.warnings[0]["row"] == 4


@pytest.mark.parametrize(
    "source",
    [
        'name,sku\n"unterminated,A-1\n',
        'name,sku\n"Ada"x,A-1\n',
    ],
)
def test_a_malformed_quote_raises_a_record_error_not_a_csv_error(source: str) -> None:
    # csv.Error is an untyped exception at the API boundary, which is a 500.
    with pytest.raises(RecordError, match="malformed"):
        delimited(source)


def test_the_process_wide_csv_field_size_limit_is_restored_after_a_failure() -> None:
    before = csv.field_size_limit()
    with pytest.raises(RecordError):
        delimited('name\n"unterminated\n')
    assert csv.field_size_limit() == before


def test_a_newline_inside_a_quoted_field_is_preserved() -> None:
    report = delimited('name,address\r\nAda,"12 Engine Way\nLondon"\r\n')
    assert report.document.records[0]["address"] == "12 Engine Way\nLondon"


def test_too_many_delimited_rows_raises_limit_exceeded() -> None:
    with pytest.raises(LimitExceeded) as caught:
        delimited("name\nAda\nGrace\nAlan\n", max_records=2)
    assert caught.value.limit_name == "max_records"
    assert caught.value.limit == 2


def test_an_over_long_cell_names_the_row_and_field() -> None:
    with pytest.raises(RecordError) as caught:
        delimited("name,sku\nAda,A-1\nfar too long,A-2\n", max_value_length=5)
    assert "record 3" in caught.value.message
    assert "'name'" in caught.value.message


def test_a_header_no_template_can_reference_warns_but_keeps_the_column() -> None:
    report = delimited("first name,sku\nAda,A-1\n")
    assert "invalid_field_name" in codes(report)
    assert report.document.records[0]["first name"] == "Ada"


# --- uploads --------------------------------------------------------------


def test_json_content_in_a_file_named_csv_is_parsed_as_json() -> None:
    # The old code chose the parser from the extension, which the client sends.
    report = upload(b'[{"name": "Ada", "sku": "A-1"}]', "data.csv")
    assert report.detected["format"] == "json"
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1"}


def test_delimited_content_in_a_file_named_json_is_parsed_as_delimited() -> None:
    report = upload(b"name,sku\nAda,A-1\n", "data.json")
    assert report.detected["format"] == "delimited"
    assert report.document.records[0] == {"name": "Ada", "sku": "A-1"}


@pytest.mark.parametrize("filename", [None, "", "data", "data.weird"])
def test_the_format_is_decided_by_content_even_with_no_usable_filename(
    filename: str | None,
) -> None:
    assert upload(b'[{"name": "Ada"}]', filename).detected["format"] == "json"
    assert upload(b"name\nAda\n", filename).detected["format"] == "delimited"


def test_a_csv_whose_first_cell_opens_with_a_brace_falls_back_to_the_extension() -> None:
    report = upload(b"{code,sku\n{a},A-1\n", "data.csv")
    assert report.detected["format"] == "delimited"
    assert report.document.records[0]["{code"] == "{a}"


def test_a_json_file_with_a_syntax_error_keeps_its_syntax_error() -> None:
    with pytest.raises(RecordError, match="not valid JSON"):
        upload(b'[{"name": "Ada",]', "data.json")


def test_utf8_with_a_bom_is_the_primary_encoding() -> None:
    report = upload("﻿name,sku\nAda,A-1\n".encode(), "data.csv")
    assert report.detected["encoding"] == "utf-8-sig"
    assert report.document.schema == ("name", "sku")
    assert report.warnings == []


def test_a_windows_1252_export_is_decoded_with_a_warning_rather_than_refused() -> None:
    report = upload("name\nCafé\n".encode("cp1252"), "data.csv")
    assert report.detected["encoding"] == "cp1252"
    assert codes(report) == ["fallback_encoding"]
    assert report.document.records[0]["name"] == "Café"


def test_bytes_that_decode_as_neither_encoding_raise_a_record_error() -> None:
    with pytest.raises(RecordError, match="UTF-8"):
        upload(b"name\n\x81\x8d\x8f\x90\n", "data.csv")


def test_a_binary_file_is_refused_before_any_parsing() -> None:
    with pytest.raises(RecordError) as caught:
        upload(b"PK\x03\x04\x00\x00name,sku", "data.xlsx")
    assert caught.value.loc == ("file",)


@pytest.mark.parametrize("data", [b"", b"   ", b"\xef\xbb\xbf   "])
def test_an_empty_upload_is_refused(data: bytes) -> None:
    with pytest.raises(RecordError, match="empty"):
        upload(data, "data.csv")


def test_the_template_fields_lead_the_uploaded_schema() -> None:
    report = upload(b"note,name\nx,Ada\n", "data.csv", fields=["name", "sku"])
    assert report.document.schema == ("name", "sku", "note")


def test_upload_warnings_survive_the_schema_reordering() -> None:
    report = upload("name,name\nCaf\u00e9,Lovelace\n".encode("cp1252"), "data.csv")
    assert codes(report) == ["fallback_encoding", "duplicate_header"]


def test_an_oversized_upload_reports_the_limit_rather_than_the_format() -> None:
    with pytest.raises(LimitExceeded) as caught:
        upload(b'[{"a": "1"}, {"a": "2"}]', "data.csv", max_records=1)
    assert caught.value.limit_name == "max_records"
