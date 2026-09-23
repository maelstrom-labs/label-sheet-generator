"""The template catalogue and the built-in Avery stock table."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from label_sheet_generator import avery, geometry
from label_sheet_generator.catalog import Catalog, parse_template_document
from label_sheet_generator.errors import NotFoundError, TemplateError, UnsafePathError
from label_sheet_generator.schema import LabelTemplate, TextLayoutTemplate
from label_sheet_generator.settings import Settings

PRESETS = avery.iter_presets()
PRESET_IDS = [preset.code for preset in PRESETS]

#: Every key the frontend thumbnail reads out of ``entry.geometry``.
GEOMETRY_KEYS = {
    "page_width_mm",
    "page_height_mm",
    "rows",
    "cols",
    "label_width_mm",
    "label_height_mm",
    "gap_x_mm",
    "gap_y_mm",
    "margin_top_mm",
    "margin_right_mm",
    "margin_bottom_mm",
    "margin_left_mm",
}

LABEL_TEMPLATE = {
    "template_type": "label",
    "name": "user-copy",
    "page": {"width_mm": 100, "height_mm": 100},
    "grid": {"rows": 2, "cols": 2, "label_width_mm": 50, "label_height_mm": 50},
}


def write_template(root: Path, relative: str, payload: Any) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def with_user_root(settings: Settings, root: Path) -> Settings:
    return replace(settings, user_template_root=root)


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    root = tmp_path / "user-templates"
    root.mkdir()
    return root


# --------------------------------------------------------------------------
# Building the catalogue from the shipped data
# --------------------------------------------------------------------------


def test_build_indexes_every_shipped_builtin_and_breaks_on_none(catalog: Catalog) -> None:
    builtin_ids = {
        entry.id
        for entry in catalog.label_templates() + catalog.layout_templates()
        if entry.source == "builtin"
    }
    assert builtin_ids == {
        "labels/basic-address",
        "labels/basic-address-inch",
        "labels/spice-jar",
        "layouts/basic-address",
        "layouts/spice-jar",
    }
    assert catalog.broken == []


def test_shipped_labels_and_layouts_are_classified_by_their_document_type(
    catalog: Catalog,
) -> None:
    assert all(isinstance(entry.template, LabelTemplate) for entry in catalog.label_templates())
    assert all(
        isinstance(entry.template, TextLayoutTemplate) for entry in catalog.layout_templates()
    )


def test_catalog_membership_and_length_cover_every_entry(catalog: Catalog) -> None:
    assert "labels/basic-address" in catalog
    assert "labels/nope" not in catalog
    assert len(catalog) == len(catalog.label_templates()) + len(catalog.layout_templates())


def test_presets_can_be_left_out_of_the_catalog(settings: Settings) -> None:
    without = Catalog.build(settings, include_presets=False)
    assert without.preset_ids == frozenset()
    assert not [entry for entry in without.label_templates() if entry.source == "preset"]


# --------------------------------------------------------------------------
# Layering a user directory over the built-ins
# --------------------------------------------------------------------------


def test_user_templates_are_layered_on_top_of_the_builtins(
    settings: Settings, user_root: Path
) -> None:
    write_template(user_root, "labels/mine.json", LABEL_TEMPLATE)
    catalog = Catalog.build(with_user_root(settings, user_root))

    entry = catalog.get("labels/mine")
    assert entry.source == "user"
    assert "labels/basic-address" in catalog


def test_a_user_template_shadows_the_builtin_with_the_same_id(
    settings: Settings, user_root: Path
) -> None:
    write_template(user_root, "labels/basic-address.json", LABEL_TEMPLATE)
    catalog = Catalog.build(with_user_root(settings, user_root))

    entry = catalog.get("labels/basic-address")
    assert entry.source == "user"
    assert entry.name == "user-copy"
    assert entry.labels_per_page == 4


def test_shadowing_a_builtin_is_recorded_as_a_warning_on_the_survivor(
    settings: Settings, user_root: Path
) -> None:
    write_template(user_root, "labels/basic-address.json", LABEL_TEMPLATE)
    catalog = Catalog.build(with_user_root(settings, user_root))

    warnings = catalog.get("labels/basic-address").warnings
    assert any("shadow" in warning for warning in warnings), warnings


def test_a_user_template_with_a_fresh_id_carries_no_shadow_warning(
    settings: Settings, user_root: Path
) -> None:
    write_template(user_root, "labels/mine.json", LABEL_TEMPLATE)
    catalog = Catalog.build(with_user_root(settings, user_root))

    assert catalog.get("labels/mine").warnings == ()


# --------------------------------------------------------------------------
# Broken files are reported, not swallowed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "payload", "expected"),
    [
        ("labels/truncated.json", "{ not json at all", "not valid JSON"),
        (
            "labels/bad-field.json",
            {
                "template_type": "label",
                "page": {"width_mm": -5, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
            },
            "page.width_mm",
        ),
        ("labels/unrecognised.json", {"colour": "red"}, "not a recognised template"),
        ("labels/wrong-type.json", ["a", "list"], "must be a JSON object"),
    ],
    ids=["invalid-json", "schema-violation", "no-discriminator", "not-an-object"],
)
def test_a_malformed_template_becomes_a_broken_entry_explaining_why(
    settings: Settings, user_root: Path, relative: str, payload: Any, expected: str
) -> None:
    # The old index caught OSError/TemplateError per file and did `continue`,
    # so a template with one bad key simply vanished from the listing with no
    # way to find out which file was at fault or why.
    write_template(user_root, relative, payload)
    catalog = Catalog.build(with_user_root(settings, user_root))

    entry_id = relative.removesuffix(".json")
    broken = {item.id: item for item in catalog.broken}
    assert entry_id in broken, catalog.broken
    assert expected in broken[entry_id].message
    assert broken[entry_id].path == relative


def test_a_broken_file_does_not_stop_its_siblings_from_loading(
    settings: Settings, user_root: Path
) -> None:
    write_template(user_root, "labels/truncated.json", "{")
    write_template(user_root, "labels/fine.json", LABEL_TEMPLATE)
    catalog = Catalog.build(with_user_root(settings, user_root))

    assert "labels/fine" in catalog
    assert "labels/truncated" not in catalog
    assert [item.id for item in catalog.broken] == ["labels/truncated"]


def test_a_broken_entry_message_never_leaks_an_absolute_filesystem_path(
    settings: Settings, user_root: Path, tmp_path: Path
) -> None:
    write_template(user_root, "labels/truncated.json", "{ not json")
    catalog = Catalog.build(with_user_root(settings, user_root))

    broken = catalog.broken[0]
    assert str(tmp_path) not in broken.message
    assert str(tmp_path) not in broken.path
    assert not any(token.startswith("/") for token in broken.message.split())


def test_a_broken_entry_serialises_for_the_api(settings: Settings, user_root: Path) -> None:
    write_template(user_root, "labels/truncated.json", "{")
    catalog = Catalog.build(with_user_root(settings, user_root))

    payload = catalog.broken[0].to_dict()
    assert set(payload) == {"id", "path", "message"}
    assert json.loads(json.dumps(payload)) == payload


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------


def test_get_returns_the_entry_for_a_known_id(catalog: Catalog, basic_template_id: str) -> None:
    assert catalog.get(basic_template_id).id == basic_template_id


def test_get_tolerates_surrounding_whitespace(catalog: Catalog, basic_template_id: str) -> None:
    assert catalog.get(f"  {basic_template_id}  ").id == basic_template_id


def test_get_raises_not_found_for_an_unknown_id(catalog: Catalog) -> None:
    with pytest.raises(NotFoundError):
        catalog.get("labels/does-not-exist")


@pytest.mark.parametrize(
    "hostile_id",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "labels/../../../etc/passwd",
        "..\\..\\windows\\system32",
        "file:///etc/passwd",
        "avery:../../etc/passwd",
    ],
)
def test_get_refuses_a_traversal_attempt_without_reading_anything(
    catalog: Catalog, hostile_id: str
) -> None:
    with pytest.raises((NotFoundError, UnsafePathError)):
        catalog.get(hostile_id)


def test_a_not_found_message_clips_an_enormous_hostile_id(catalog: Catalog) -> None:
    # The id is echoed back so a typo is findable, but a megabyte of attacker
    # text must not be reflected verbatim into the response or the log.
    huge = "z" * 5_000
    with pytest.raises(NotFoundError) as excinfo:
        catalog.get(huge)
    assert len(excinfo.value.message) < 500
    assert "..." in excinfo.value.message


# --------------------------------------------------------------------------
# Presets inside the catalogue
# --------------------------------------------------------------------------


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_every_preset_appears_as_a_preset_sourced_entry(
    catalog: Catalog, preset: avery.AveryPreset
) -> None:
    entry = catalog.get(f"avery:{preset.code}")
    assert entry.id == f"avery:{preset.code.lower()}"
    assert entry.source == "preset"
    assert entry.kind == "label"
    assert entry.path is None
    assert entry.labels_per_page == preset.labels_per_sheet


def test_preset_ids_are_reported_as_the_preset_subset(catalog: Catalog) -> None:
    assert catalog.preset_ids == frozenset(f"avery:{preset.code.lower()}" for preset in PRESETS)


def test_a_preset_entry_is_reachable_through_an_alias(catalog: Catalog) -> None:
    assert catalog.get("avery:5260").id == "avery:5160"


def test_a_preset_entry_exposes_its_description_and_aliases(catalog: Catalog) -> None:
    entry = catalog.get("avery:5160")
    assert entry.aliases == ("5260", "5960", "8160")
    assert "2-5/8" in (entry.description or "")


def test_an_unknown_preset_code_is_not_found(catalog: Catalog) -> None:
    with pytest.raises(NotFoundError):
        catalog.get("avery:9999")


# --------------------------------------------------------------------------
# Entry shape
# --------------------------------------------------------------------------


def test_every_label_entry_publishes_the_full_geometry_block(catalog: Catalog) -> None:
    for entry in catalog.label_templates():
        assert entry.geometry is not None, entry.id
        assert set(entry.geometry) == GEOMETRY_KEYS, entry.id


def test_a_layout_entry_has_no_geometry_and_no_labels_per_page(catalog: Catalog) -> None:
    for entry in catalog.layout_templates():
        assert entry.geometry is None
        assert entry.labels_per_page == 0


def test_the_geometry_block_keeps_an_exact_inch_fraction(catalog: Catalog) -> None:
    # 66.675mm is exactly 2-5/8in. Rounding the block to 2dp would store 66.68
    # and drift the third column of the sheet.
    geometry_block = catalog.get("avery:5160").geometry
    assert geometry_block is not None
    assert geometry_block["label_width_mm"] == pytest.approx(66.675, abs=1e-9)


def test_geometry_reports_the_derived_label_size_when_the_template_omits_it(
    settings: Settings, user_root: Path
) -> None:
    write_template(
        user_root,
        "labels/derived.json",
        {
            "template_type": "label",
            "page": {"width_mm": 100, "height_mm": 100},
            "grid": {"rows": 2, "cols": 2, "margin_left_mm": 10, "margin_right_mm": 10},
        },
    )
    catalog = Catalog.build(with_user_root(settings, user_root))

    geometry_block = catalog.get("labels/derived").geometry
    assert geometry_block is not None
    assert geometry_block["label_width_mm"] == pytest.approx(40)
    assert geometry_block["label_height_mm"] == pytest.approx(50)


def test_a_template_whose_grid_overruns_the_page_is_listed_with_the_complaint(
    settings: Settings, user_root: Path
) -> None:
    # The file parses, so it belongs in the listing -- but it must not be
    # presented as if it were printable.
    write_template(
        user_root,
        "labels/too-wide.json",
        {
            "template_type": "label",
            "page": {"width_mm": 100, "height_mm": 100},
            "grid": {"rows": 1, "cols": 2, "label_width_mm": 60, "label_height_mm": 50},
        },
    )
    catalog = Catalog.build(with_user_root(settings, user_root))

    entry = catalog.get("labels/too-wide")
    assert catalog.broken == []
    assert entry.warnings, "a grid running off the page must be reported"


def test_entry_fields_are_the_templates_field_names(catalog: Catalog) -> None:
    for entry in catalog.label_templates() + catalog.layout_templates():
        assert list(entry.fields) == entry.template.field_names, entry.id


def test_entry_fields_follow_element_order_with_name_first(catalog: Catalog) -> None:
    assert catalog.get("layouts/basic-address").fields == (
        "name",
        "address_1",
        "address_2",
        "sku",
    )


def test_entry_to_dict_is_json_serialisable_with_the_listing_keys(
    catalog: Catalog, basic_template_id: str
) -> None:
    payload = catalog.get(basic_template_id).to_dict()
    assert set(payload) == {
        "id",
        "kind",
        "name",
        "source",
        "fields",
        "geometry",
        "labels_per_page",
        "units",
        "description",
        "aliases",
        "warnings",
    }
    assert json.loads(json.dumps(payload)) == payload


def test_a_listing_entry_never_exposes_a_filesystem_path(
    catalog: Catalog, basic_template_id: str
) -> None:
    assert "path" not in catalog.get(basic_template_id).to_dict()


# --------------------------------------------------------------------------
# parse_template_document: every legacy shape still loads
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            {
                "template_type": "label",
                "page": {"width_mm": 100, "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
            },
            "label",
        ),
        ({"template_type": "text-layout", "elements": []}, "text-layout"),
        (
            {"page": {"width_mm": 100, "height_mm": 100}, "grid": {"rows": 1, "cols": 1}},
            "label",
        ),
        (
            {"elements": [{"type": "text", "x_mm": 1, "y_mm": 1, "width_mm": 10, "height_mm": 5}]},
            "text-layout",
        ),
    ],
    ids=["explicit-label", "explicit-layout", "sniffed-label", "sniffed-layout"],
)
def test_parse_accepts_every_legacy_document_shape(document: dict[str, Any], expected: str) -> None:
    assert parse_template_document(document).template_type == expected


def test_parse_keeps_an_inch_authored_document_in_inches() -> None:
    parsed = parse_template_document(
        {
            "page": {"width_in": 8.5, "height_in": 11.0},
            "grid": {"rows": 10, "cols": 3, "label_width_in": 2.625, "label_height_in": 1.0},
        }
    )
    assert parsed.units == "in"
    assert parsed.page.width_mm == pytest.approx(215.9)


def test_parse_accepts_inch_keys_in_a_layout_document() -> None:
    parsed = parse_template_document(
        {
            "elements": [
                {"type": "text", "x_in": 0.1, "y_in": 0.1, "width_in": 1.0, "height_in": 0.5}
            ]
        }
    )
    assert parsed.units == "in"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({}, "not a recognised template"),
        ({"colour": "red"}, "not a recognised template"),
        ({"template_type": "sheet", "elements": []}, "expected 'label' or 'text-layout'"),
        ({"template_type": 7, "elements": []}, "non-string template_type"),
        ({"template_type": "label"}, "not a valid label template"),
        ("just a string", "must be a JSON object"),
        ([1, 2, 3], "must be a JSON object"),
    ],
    ids=["empty", "unknown-keys", "bad-type", "non-string-type", "missing-page", "str", "list"],
)
def test_parse_rejects_garbage_as_a_template_error(document: Any, expected: str) -> None:
    # A raw pydantic ValidationError escaping here became an HTTP 500 rather
    # than a 422, so every failure is normalised to TemplateError.
    with pytest.raises(TemplateError) as excinfo:
        parse_template_document(document)
    assert expected in excinfo.value.message


def test_a_schema_failure_carries_field_level_details() -> None:
    with pytest.raises(TemplateError) as excinfo:
        parse_template_document(
            {
                "template_type": "label",
                "page": {"width_mm": "wide", "height_mm": 100},
                "grid": {"rows": 1, "cols": 1},
            }
        )
    details = excinfo.value.details
    assert details, "field-level details must survive the conversion"
    assert ["page", "width_mm"] in [detail["loc"] for detail in details]
    assert all({"loc", "msg", "type"} <= set(detail) for detail in details)
    assert json.loads(json.dumps(details)) == details


def test_a_multi_problem_document_says_how_many_problems_there_are() -> None:
    with pytest.raises(TemplateError) as excinfo:
        parse_template_document(
            {
                "template_type": "label",
                "page": {"width_mm": -1, "height_mm": -1},
                "grid": {"rows": 0, "cols": 0},
            }
        )
    assert "more" in excinfo.value.message
    assert len(excinfo.value.details) > 1


def test_parse_labels_the_document_by_the_name_the_caller_gives_it() -> None:
    with pytest.raises(TemplateError) as excinfo:
        parse_template_document({}, what="template labels/mine.json")
    assert "labels/mine.json" in excinfo.value.message


# --------------------------------------------------------------------------
# Avery presets
# --------------------------------------------------------------------------


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_preset_closes_on_the_x_axis(preset: avery.AveryPreset) -> None:
    span = (
        preset.margin_left_in
        + preset.cols * preset.label_width_in
        + (preset.cols - 1) * preset.gap_x_in
        + preset.margin_right_in
    )
    assert span == pytest.approx(preset.page_width_in, abs=0.01)


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_preset_closes_on_the_y_axis(preset: avery.AveryPreset) -> None:
    span = (
        preset.margin_top_in
        + preset.rows * preset.label_height_in
        + (preset.rows - 1) * preset.gap_y_in
        + preset.margin_bottom_in
    )
    assert span == pytest.approx(preset.page_height_in, abs=0.01)


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_preset_geometry_is_clean_in_millimetres_too(preset: avery.AveryPreset) -> None:
    report = geometry.validate(avery.build_template(preset.code))
    assert report.errors == []
    assert report.warnings == []


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_preset_millimetre_properties_are_derived_from_the_authored_inches(
    preset: avery.AveryPreset,
) -> None:
    assert preset.label_width_mm == pytest.approx(preset.label_width_in * 25.4, abs=1e-6)
    assert preset.page_height_mm == pytest.approx(preset.page_height_in * 25.4, abs=1e-6)


def test_avery_5160_keeps_the_exact_inch_fraction_of_its_label_width() -> None:
    # The old table stored a pre-rounded 66.67mm literal. 2-5/8in is exactly
    # 66.675mm, and the 5 micron error per label moved the third column by a
    # visible fraction of a millimetre across the sheet.
    preset = avery.get_preset("5160")
    assert preset.label_width_in == 2.625
    assert preset.label_width_mm == pytest.approx(66.675, abs=1e-9)
    assert preset.label_width_mm != pytest.approx(66.68, abs=1e-4)


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("5160", "5160"),
        ("5260", "5160"),
        ("5960", "5160"),
        ("8160", "5160"),
        ("5262", "5162"),
        ("J8160", "L7160"),
    ],
)
def test_get_preset_resolves_an_alias_to_its_canonical_preset(typed: str, canonical: str) -> None:
    assert avery.get_preset(typed).code == canonical


@pytest.mark.parametrize("typed", ["l7160", "L7160", " l7160 ", "\tL7160\n", "L-7160", "L 7160"])
def test_get_preset_ignores_case_whitespace_and_punctuation(typed: str) -> None:
    assert avery.get_preset(typed).code == "L7160"


@pytest.mark.parametrize("typed", ["9999", "", "   ", "not-a-code", "5160x"])
def test_get_preset_raises_not_found_for_an_unknown_code(typed: str) -> None:
    with pytest.raises(NotFoundError):
        avery.get_preset(typed)


def test_the_not_found_message_lists_the_codes_that_do_exist() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        avery.get_preset("9999")
    assert "5160" in excinfo.value.message


def test_no_code_or_alias_is_claimed_by_two_presets() -> None:
    claimed: dict[str, str] = {}
    for preset in PRESETS:
        for code in preset.codes:
            normalized = avery.normalize_code(code)
            assert normalized not in claimed, (
                f"{code} is claimed by both {claimed.get(normalized)} and {preset.code}"
            )
            claimed[normalized] = preset.code
    assert len(claimed) == sum(len(preset.codes) for preset in PRESETS)


def test_a_preset_never_lists_its_own_canonical_code_as_an_alias() -> None:
    for preset in PRESETS:
        assert preset.code not in preset.aliases


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_build_template_authors_the_sheet_in_inches(preset: avery.AveryPreset) -> None:
    template = avery.build_template(preset.code)
    assert template.units == "in"

    dumped = template.dump()
    assert dumped["page"]["width_in"] == pytest.approx(preset.page_width_in)
    assert "width_mm" not in dumped["page"]
    assert dumped["grid"]["label_width_in"] == pytest.approx(preset.label_width_in)


@pytest.mark.parametrize("preset", PRESETS, ids=PRESET_IDS)
def test_build_template_grid_holds_rows_times_cols_labels(
    preset: avery.AveryPreset,
) -> None:
    template = avery.build_template(preset.code)
    assert template.grid.rows == preset.rows
    assert template.grid.cols == preset.cols
    assert template.grid.cells_per_page == preset.rows * preset.cols
    assert template.grid.cells_per_page == preset.labels_per_sheet


def test_build_template_round_trips_through_its_own_dump() -> None:
    template = avery.build_template("5160")
    assert LabelTemplate.model_validate(template.dump()).dump() == template.dump()


def test_build_template_records_the_canonical_code_behind_an_alias() -> None:
    template = avery.build_template("5260")
    assert template.metadata["preset_canonical_code"] == "5160"
    assert template.metadata["preset_code"] == "5260"
    assert template.metadata["preset_brand"] == "Avery"


def test_build_template_accepts_a_caller_supplied_name() -> None:
    assert avery.build_template("5160", name="mailing-run").name == "mailing-run"


def test_build_template_names_itself_after_the_preset_by_default() -> None:
    assert avery.build_template("L7160").name == "avery-l7160"


def test_build_template_rejects_an_unknown_code() -> None:
    with pytest.raises(NotFoundError):
        avery.build_template("9999")


def test_a_preset_carries_no_elements_so_a_layout_supplies_the_content() -> None:
    assert avery.build_template("5160").elements == []
    assert avery.build_template("5160").field_names == []


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("5160", "5160"), (" 5160 ", "5160"), ("l7160", "L7160"), ("5-160", "5160")],
)
def test_normalize_code_folds_case_and_punctuation(typed: str, expected: str) -> None:
    assert avery.normalize_code(typed) == expected
