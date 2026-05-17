from label_sheet_generator.avery import build_template_from_avery_preset, get_avery_preset


def test_get_avery_preset_supports_alias_codes() -> None:
    preset = get_avery_preset("8160")

    assert preset.canonical_code == "5160"
    assert preset.rows == 10
    assert preset.cols == 3


def test_build_template_from_avery_preset_sets_geometry_and_metadata() -> None:
    template = build_template_from_avery_preset("5163")

    assert template.grid.rows == 5
    assert template.grid.cols == 2
    assert template.grid.label_width_mm == 101.6
    assert template.metadata["preset_brand"] == "Avery"
    assert template.metadata["preset_code"] == "5163"
