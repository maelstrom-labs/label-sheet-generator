"""The command line interface: exit codes, output, and the failure paths.

Every command is driven through ``main(argv)`` rather than a subprocess, so a
crash surfaces as a real traceback in the test report instead of a return code.
The ``cli`` fixture asserts on every single invocation that nothing resembling
a Python traceback reached stderr: the CLI's contract is that a bad argument
produces a one-line message, never a stack dump.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from label_sheet_generator import __version__
from label_sheet_generator.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main
from label_sheet_generator.schema import LabelTemplate

pypdf = pytest.importorskip("pypdf")

TRACEBACK_MARKER = "Traceback (most recent call last)"

#: Fields the packaged spice-jar template prints, and its one-label-per-sheet
#: grid, which is what makes page count equal record count.
SPICE_FIELDS = ("name", "category", "descriptor")
SPICE_TEMPLATE = "labels/spice-jar"
ADDRESS_TEMPLATE = "labels/basic-address"


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


Cli = Callable[..., Run]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    """Invoke ``main`` and capture the result, refusing any traceback."""

    def invoke(*argv: str) -> Run:
        try:
            code = main(list(argv))
        except SystemExit as exit_request:  # argparse exits for --version/--help
            code = 0 if exit_request.code is None else int(exit_request.code)
        captured = capsys.readouterr()
        assert TRACEBACK_MARKER not in captured.err, f"{argv} printed a traceback:\n{captured.err}"
        assert TRACEBACK_MARKER not in captured.out, f"{argv} printed a traceback:\n{captured.out}"
        return Run(code=code, out=captured.out, err=captured.err)

    return invoke


def write_template(directory: Path, name: str, document: dict[str, Any]) -> Path:
    path = directory / f"{name}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def grid_template(**grid: Any) -> dict[str, Any]:
    """A 100x100mm one-up sheet whose grid can be pushed off the page."""
    base: dict[str, Any] = {
        "rows": 1,
        "cols": 1,
        "label_width_mm": 60,
        "label_height_mm": 60,
    }
    base.update(grid)
    return {"page": {"width_mm": 100, "height_mm": 100}, "grid": base}


def records_json(path: Path, count: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": list(SPICE_FIELDS),
                "records": [
                    {"name": f"Jar {index}", "category": "Spice", "descriptor": "Ground"}
                    for index in range(count)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def records_csv(path: Path, count: int) -> Path:
    lines = [",".join(SPICE_FIELDS)]
    lines.extend(f"Jar {index},Spice,Ground" for index in range(count))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_pdf(path: Path) -> Any:
    return pypdf.PdfReader(str(path))


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in read_pdf(path).pages)


# --- top level ------------------------------------------------------------


def test_version_flag_exits_zero_and_prints_the_version(cli: Cli) -> None:
    result = cli("--version")
    assert result.code == EXIT_OK
    assert __version__ in result.out


def test_invoking_no_command_at_all_is_a_usage_error(cli: Cli) -> None:
    result = cli()
    assert result.code == EXIT_USAGE
    assert "usage:" in result.err


def test_an_unknown_subcommand_is_a_usage_error(cli: Cli) -> None:
    result = cli("frobnicate")
    assert result.code == EXIT_USAGE
    assert "invalid choice" in result.err


# --- list-templates -------------------------------------------------------


@pytest.mark.parametrize(
    "template_id",
    [
        "labels/basic-address",
        "labels/basic-address-inch",
        "labels/spice-jar",
        "avery:5160",
        "avery:5161",
        "avery:5162",
        "avery:5163",
        "avery:5164",
        "avery:l7160",
        "avery:l7163",
    ],
)
def test_list_templates_names_every_builtin_and_avery_sheet(cli: Cli, template_id: str) -> None:
    result = cli("list-templates")
    assert result.code == EXIT_OK
    assert template_id in result.out


@pytest.mark.parametrize("layout_id", ["layouts/basic-address", "layouts/spice-jar"])
def test_list_templates_lists_text_layouts_as_well(cli: Cli, layout_id: str) -> None:
    result = cli("list-templates")
    assert layout_id in result.out


def test_list_templates_json_emits_templates_layouts_and_broken(cli: Cli) -> None:
    result = cli("list-templates", "--json")
    assert result.code == EXIT_OK
    payload = json.loads(result.out)
    assert set(payload) == {"templates", "layouts", "broken"}
    assert {entry["id"] for entry in payload["templates"]} >= {
        ADDRESS_TEMPLATE,
        SPICE_TEMPLATE,
        "avery:5160",
    }
    assert {entry["id"] for entry in payload["layouts"]} == {
        "layouts/basic-address",
        "layouts/spice-jar",
    }


def test_list_templates_json_reports_an_unloadable_file_instead_of_hiding_it(
    cli: Cli, tmp_path: Path
) -> None:
    # The old preset index swallowed the parse error and dropped the file, so
    # a template with one bad key simply vanished with no way to find out why.
    root = tmp_path / "templates"
    (root / "labels").mkdir(parents=True)
    (root / "labels" / "rotten.json").write_text("{", encoding="utf-8")

    result = cli("--templates", str(root), "list-templates", "--json")
    assert result.code == EXIT_OK
    broken = json.loads(result.out)["broken"]
    assert [entry["id"] for entry in broken] == ["labels/rotten"]
    assert "JSON" in broken[0]["message"]


def test_a_user_template_directory_is_layered_over_the_builtins(cli: Cli, tmp_path: Path) -> None:
    root = tmp_path / "templates"
    (root / "labels").mkdir(parents=True)
    write_template(root / "labels", "mine", grid_template())

    result = cli("--templates", str(root), "list-templates")
    assert result.code == EXIT_OK
    assert "labels/mine" in result.out
    assert ADDRESS_TEMPLATE in result.out


# --- check ----------------------------------------------------------------


@pytest.mark.parametrize("reference", [ADDRESS_TEMPLATE, SPICE_TEMPLATE, "avery:5160"])
def test_check_on_a_sound_template_exits_zero_and_says_ok(cli: Cli, reference: str) -> None:
    result = cli("check", reference)
    assert result.code == EXIT_OK
    assert "ok" in result.out
    assert result.err == ""


def test_check_on_a_local_template_file_exits_zero(cli: Cli, tmp_path: Path) -> None:
    path = write_template(tmp_path, "fits", grid_template(label_width_mm=50))
    result = cli("check", str(path))
    assert result.code == EXIT_OK
    assert "ok" in result.out


def test_check_on_a_grid_that_overruns_the_page_exits_one_with_the_error_on_stderr(
    cli: Cli, tmp_path: Path
) -> None:
    path = write_template(tmp_path, "toobig", grid_template(rows=2, cols=2))
    result = cli("check", str(path))
    assert result.code == EXIT_ERROR
    assert "error" in result.err
    assert "overruns" in result.err
    assert "ok" not in result.out


def test_check_reports_a_geometry_warning_without_failing(cli: Cli, tmp_path: Path) -> None:
    path = write_template(tmp_path, "sloppy", grid_template(margin_right_mm=50))
    result = cli("check", str(path))
    assert result.code == EXIT_OK
    assert "warning" in result.out


@pytest.mark.parametrize("reference", ["x" * 500, "x" * 500 + ".json", "a\x00b.json"])
def test_an_id_no_filesystem_could_hold_is_reported_not_raised(cli: Cli, reference: str) -> None:
    # Probing a free-form template id as a path called Path.exists/is_file,
    # and stat raises OSError (ENAMETOOLONG) or ValueError (embedded NUL)
    # instead of returning False, so a mistyped id escaped as a traceback.
    result = cli("check", reference)
    assert result.code == EXIT_USAGE
    assert result.err.startswith("error:")


def test_an_output_path_that_cannot_be_written_is_reported_not_raised(
    cli: Cli, tmp_path: Path
) -> None:
    # The destination directory is really a regular file, so the atomic write
    # fails with NotADirectoryError; it used to reach the shell as a traceback.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    result = cli("generate", SPICE_TEMPLATE, str(blocker / "sheet.pdf"))
    assert result.code == EXIT_ERROR
    assert "could not write" in result.err


def test_check_on_an_unknown_template_id_exits_two_with_a_message(cli: Cli) -> None:
    result = cli("check", "no-such-template")
    assert result.code == EXIT_USAGE
    assert "no-such-template" in result.err
    assert "not found" in result.err


def test_check_on_a_template_path_that_does_not_exist_exits_two(cli: Cli, tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    result = cli("check", str(missing))
    assert result.code == EXIT_USAGE
    assert str(missing) in result.err


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{", "JSON"),
        ("[1, 2]", "JSON object"),
        ('{"page": {"width_mm": "wide"}, "grid": {"rows": 1, "cols": 1}}', "page"),
        ('{"nothing": true}', "not a recognised template"),
    ],
)
def test_check_on_a_malformed_template_file_exits_two(
    cli: Cli, tmp_path: Path, content: str, expected: str
) -> None:
    path = tmp_path / "junk.json"
    path.write_text(content, encoding="utf-8")
    result = cli("check", str(path))
    assert result.code == EXIT_USAGE
    assert expected in result.err


# --- generate -------------------------------------------------------------


def test_generate_without_records_writes_a_pdf(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    result = cli("generate", ADDRESS_TEMPLATE, str(output))
    assert result.code == EXIT_OK
    assert output.read_bytes().startswith(b"%PDF")
    assert str(output) in result.out


def test_generate_without_records_still_fills_one_whole_sheet(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    cli("generate", ADDRESS_TEMPLATE, str(output))
    assert len(read_pdf(output).pages) == 1


@pytest.mark.parametrize("writer", [records_json, records_csv])
@pytest.mark.parametrize("count", [1, 3])
def test_generate_page_count_matches_the_record_count_on_a_one_up_sheet(
    cli: Cli, tmp_path: Path, writer: Callable[[Path, int], Path], count: int
) -> None:
    records = writer(tmp_path / f"records{writer.__name__[-4:]}", count)
    output = tmp_path / "sheet.pdf"
    result = cli("generate", SPICE_TEMPLATE, str(output), "--records", str(records))
    assert result.code == EXIT_OK
    assert len(read_pdf(output).pages) == count


def test_generate_paginates_records_across_sheets(cli: Cli, tmp_path: Path) -> None:
    # basic-address holds 30 labels per sheet, so 31 records must spill over.
    path = tmp_path / "many.json"
    path.write_text(
        json.dumps({"schema": ["name"], "records": [{"name": str(i)} for i in range(31)]}),
        encoding="utf-8",
    )
    output = tmp_path / "sheet.pdf"
    assert cli("generate", ADDRESS_TEMPLATE, str(output), "--records", str(path)).code == EXIT_OK
    assert len(read_pdf(output).pages) == 2


def test_generate_renders_record_values_into_the_pdf(cli: Cli, tmp_path: Path) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps({"schema": list(SPICE_FIELDS), "records": [{"name": "Zanzibar"}]}),
        encoding="utf-8",
    )
    output = tmp_path / "sheet.pdf"
    cli("generate", SPICE_TEMPLATE, str(output), "--records", str(path))
    assert "Zanzibar" in pdf_text(output)


def test_generate_refuses_to_overwrite_an_existing_output(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    output.write_bytes(b"original")
    result = cli("generate", ADDRESS_TEMPLATE, str(output))
    assert result.code == EXIT_USAGE
    assert "--force" in result.err
    assert output.read_bytes() == b"original"


def test_generate_force_overwrites_an_existing_output(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    output.write_bytes(b"original")
    result = cli("generate", ADDRESS_TEMPLATE, str(output), "--force")
    assert result.code == EXIT_OK
    assert output.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("filename", ["sheet.txt", "sheet", "sheet.pdf.json"])
def test_generate_rejects_an_output_path_that_is_not_a_pdf(
    cli: Cli, tmp_path: Path, filename: str
) -> None:
    output = tmp_path / filename
    result = cli("generate", ADDRESS_TEMPLATE, str(output))
    assert result.code == EXIT_USAGE
    assert ".pdf" in result.err
    assert not output.exists()


def test_generate_layout_swaps_the_template_elements_in(cli: Cli, tmp_path: Path) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps({"schema": ["name", "category"], "records": [{"category": "Paprikat"}]}),
        encoding="utf-8",
    )
    output = tmp_path / "sheet.pdf"
    result = cli(
        "generate",
        ADDRESS_TEMPLATE,
        str(output),
        "--records",
        str(path),
        "--layout",
        "layouts/spice-jar",
    )
    assert result.code == EXIT_OK
    # "category" belongs to the swapped-in layout, not to basic-address.
    assert "Paprikat" in pdf_text(output)


def test_generate_without_a_layout_does_not_draw_the_layout_only_field(
    cli: Cli, tmp_path: Path
) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps({"schema": ["name", "category"], "records": [{"category": "Paprikat"}]}),
        encoding="utf-8",
    )
    output = tmp_path / "sheet.pdf"
    cli("generate", ADDRESS_TEMPLATE, str(output), "--records", str(path))
    assert "Paprikat" not in pdf_text(output)


def test_generate_rejects_an_unknown_layout_id(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    result = cli("generate", ADDRESS_TEMPLATE, str(output), "--layout", "layouts/nope")
    assert result.code == EXIT_USAGE
    assert "not found" in result.err
    assert not output.exists()


@pytest.mark.parametrize(
    ("flag", "key"),
    [("--margin-left", "margin_left_mm"), ("--margin-top", "margin_top_mm")],
)
def test_a_leading_margin_override_replaces_the_template_margin(
    cli: Cli, tmp_path: Path, flag: str, key: str
) -> None:
    path = write_template(tmp_path, "tight", grid_template(**{key: 50}))

    without_override = cli("generate", str(path), str(tmp_path / "a.pdf"))
    assert without_override.code == EXIT_USAGE
    assert "overruns" in without_override.err

    with_override = cli("generate", str(path), str(tmp_path / "b.pdf"), flag, "0")
    assert with_override.code == EXIT_OK
    assert (tmp_path / "b.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize(
    ("flag", "key"),
    [("--margin-right", "margin_right_mm"), ("--margin-bottom", "margin_bottom_mm")],
)
def test_a_trailing_margin_override_replaces_the_template_margin(
    cli: Cli, tmp_path: Path, flag: str, key: str
) -> None:
    path = write_template(tmp_path, "sloppy", grid_template(**{key: 50}))

    without_override = cli("generate", str(path), str(tmp_path / "a.pdf"))
    assert without_override.code == EXIT_OK
    assert "margin" in without_override.err

    with_override = cli("generate", str(path), str(tmp_path / "b.pdf"), flag, "0")
    assert with_override.code == EXIT_OK
    assert with_override.err == ""


def test_generate_landscape_produces_a_wider_than_tall_page(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    result = cli("generate", SPICE_TEMPLATE, str(output), "--orientation", "landscape")
    assert result.code == EXIT_OK
    page = read_pdf(output).pages[0]
    assert page.mediabox.width > page.mediabox.height


def test_generate_defaults_to_the_portrait_page_of_the_template(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    cli("generate", SPICE_TEMPLATE, str(output))
    page = read_pdf(output).pages[0]
    assert page.mediabox.height > page.mediabox.width


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_generate_page_rotation_is_recorded_on_the_page(
    cli: Cli, tmp_path: Path, degrees: int
) -> None:
    output = tmp_path / "sheet.pdf"
    result = cli("generate", SPICE_TEMPLATE, str(output), "--page-rotation", str(degrees))
    assert result.code == EXIT_OK
    assert read_pdf(output).pages[0].get("/Rotate") == degrees


def test_generate_rejects_a_page_rotation_that_is_not_a_right_angle(
    cli: Cli, tmp_path: Path
) -> None:
    result = cli("generate", SPICE_TEMPLATE, str(tmp_path / "sheet.pdf"), "--page-rotation", "45")
    assert result.code == EXIT_USAGE
    assert "invalid choice" in result.err


def test_generate_with_borders_still_renders(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    assert cli("generate", SPICE_TEMPLATE, str(output), "--borders").code == EXIT_OK
    assert output.read_bytes().startswith(b"%PDF")


def test_generate_accepts_an_avery_preset_alias_as_the_template(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    # 8160 is an alias of 5160, so the alias must resolve to the same sheet.
    result = cli("generate", "avery:8160", str(output))
    assert result.code == EXIT_OK
    assert "5160" in result.out


def test_generate_on_an_unknown_template_id_exits_two(cli: Cli, tmp_path: Path) -> None:
    result = cli("generate", "no-such-template", str(tmp_path / "sheet.pdf"))
    assert result.code == EXIT_USAGE
    assert "not found" in result.err
    assert not (tmp_path / "sheet.pdf").exists()


def test_generate_on_a_template_path_that_does_not_exist_exits_two(
    cli: Cli, tmp_path: Path
) -> None:
    missing = tmp_path / "absent.json"
    result = cli("generate", str(missing), str(tmp_path / "sheet.pdf"))
    assert result.code == EXIT_USAGE
    assert "no template file" in result.err
    assert str(missing) in result.err


def test_generate_refuses_a_text_layout_as_the_sheet_template(cli: Cli, tmp_path: Path) -> None:
    result = cli("generate", "layouts/spice-jar", str(tmp_path / "sheet.pdf"))
    assert result.code == EXIT_USAGE
    assert "text layout" in result.err


def test_generate_refuses_a_text_layout_file_as_the_sheet_template(
    cli: Cli, tmp_path: Path
) -> None:
    path = write_template(tmp_path, "layout", {"template_type": "text-layout", "elements": []})
    result = cli("generate", str(path), str(tmp_path / "sheet.pdf"))
    assert result.code == EXIT_USAGE
    assert "--layout" in result.err


def test_generate_on_a_record_file_that_does_not_exist_exits_two(cli: Cli, tmp_path: Path) -> None:
    result = cli(
        "generate",
        SPICE_TEMPLATE,
        str(tmp_path / "sheet.pdf"),
        "--records",
        str(tmp_path / "absent.csv"),
    )
    assert result.code == EXIT_USAGE
    assert "no record file" in result.err


def test_generate_on_a_malformed_record_file_exits_two(cli: Cli, tmp_path: Path) -> None:
    path = tmp_path / "records.json"
    path.write_text('{"records": [1, 2, 3]}', encoding="utf-8")
    result = cli("generate", SPICE_TEMPLATE, str(tmp_path / "sheet.pdf"), "--records", str(path))
    assert result.code == EXIT_USAGE
    assert result.err.startswith("error:")


def test_generate_on_a_grid_that_overruns_the_page_exits_two_and_writes_nothing(
    cli: Cli, tmp_path: Path
) -> None:
    path = write_template(tmp_path, "toobig", grid_template(rows=2, cols=2))
    output = tmp_path / "sheet.pdf"
    result = cli("generate", str(path), str(output))
    assert result.code == EXIT_USAGE
    assert "overruns" in result.err
    assert not output.exists()


# --- avery-template -------------------------------------------------------


@pytest.mark.parametrize("code", ["5160", "5163", "L7160", "l7160", "8160"])
def test_avery_template_writes_a_file_that_parses_back_as_a_label_template(
    cli: Cli, tmp_path: Path, code: str
) -> None:
    output = tmp_path / "preset.json"
    result = cli("avery-template", code, str(output))
    assert result.code == EXIT_OK
    template = LabelTemplate.model_validate(json.loads(output.read_text(encoding="utf-8")))
    assert template.grid.cells_per_page > 0


def test_avery_template_keeps_the_datasheet_units(cli: Cli, tmp_path: Path) -> None:
    # Presets are authored in the inches the manufacturer publishes; exporting
    # one used to round it into millimetres and lose the exact fraction.
    output = tmp_path / "preset.json"
    cli("avery-template", "5160", str(output))
    assert LabelTemplate.model_validate(json.loads(output.read_text())).units == "in"


def test_avery_template_name_option_sets_the_template_name(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "preset.json"
    cli("avery-template", "5160", str(output), "--name", "Warehouse Stock")
    assert LabelTemplate.model_validate(json.loads(output.read_text())).name == "Warehouse Stock"


def test_avery_template_refuses_to_overwrite_without_force(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "preset.json"
    output.write_text("keep me", encoding="utf-8")
    result = cli("avery-template", "5160", str(output))
    assert result.code == EXIT_USAGE
    assert "--force" in result.err
    assert output.read_text() == "keep me"


def test_avery_template_force_overwrites(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "preset.json"
    output.write_text("keep me", encoding="utf-8")
    result = cli("avery-template", "5160", str(output), "--force")
    assert result.code == EXIT_OK
    assert LabelTemplate.model_validate(json.loads(output.read_text())).grid.rows == 10


@pytest.mark.parametrize("filename", ["preset.pdf", "preset", "preset.json.txt"])
def test_avery_template_rejects_an_output_path_that_is_not_json(
    cli: Cli, tmp_path: Path, filename: str
) -> None:
    output = tmp_path / filename
    result = cli("avery-template", "5160", str(output))
    assert result.code == EXIT_USAGE
    assert ".json" in result.err
    assert not output.exists()


def test_avery_template_rejects_an_unknown_product_code(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "preset.json"
    result = cli("avery-template", "9999", str(output))
    assert result.code == EXIT_USAGE
    assert "unknown label stock preset" in result.err
    assert not output.exists()


# --- the cross-cutting contracts -----------------------------------------


def test_successful_commands_exit_zero(cli: Cli, tmp_path: Path) -> None:
    assert cli("list-templates").code == EXIT_OK
    assert cli("check", ADDRESS_TEMPLATE).code == EXIT_OK
    assert cli("generate", SPICE_TEMPLATE, str(tmp_path / "sheet.pdf")).code == EXIT_OK
    assert cli("avery-template", "5160", str(tmp_path / "preset.json")).code == EXIT_OK


def test_a_failed_validation_exits_one_not_two(cli: Cli, tmp_path: Path) -> None:
    # `check` is a report, not an argument error: the input was a perfectly
    # well-formed request to validate a template that turned out to be broken.
    path = write_template(tmp_path, "toobig", grid_template(rows=2, cols=2))
    assert cli("check", str(path)).code == EXIT_ERROR


@pytest.mark.parametrize(
    "argv",
    [
        ("check", "no-such-template"),
        ("generate", "no-such-template", "out.pdf"),
        ("generate", "layouts/spice-jar", "out.pdf"),
        ("avery-template", "9999", "out.json"),
        ("frobnicate",),
    ],
)
def test_invalid_input_exits_two(cli: Cli, tmp_path: Path, argv: tuple[str, ...]) -> None:
    resolved = [str(tmp_path / part) if part.endswith((".pdf", ".json")) else part for part in argv]
    assert cli(*resolved).code == EXIT_USAGE


@pytest.mark.parametrize(
    "argv",
    [
        ("check", "../../etc/passwd"),
        ("check", "avery:nope"),
        ("check", "{}"),
        ("check", "x" * 500),
        ("generate", "labels/basic-address", "OUT"),
        ("generate", "BAD_JSON", "OUT.pdf"),
        ("generate", "labels/basic-address", "OUT.pdf", "--records", "MISSING.csv"),
        ("generate", "labels/basic-address", "OUT.pdf", "--margin-left", "-5"),
        ("generate", "labels/basic-address", "OUT.pdf", "--text-rotation", "1e400"),
        ("avery-template", "", "OUT.json"),
        ("list-templates", "--json"),
    ],
)
def test_no_command_ever_prints_a_traceback(
    cli: Cli, tmp_path: Path, argv: tuple[str, ...]
) -> None:
    # The cli fixture asserts the absence of a traceback on every invocation;
    # this case list exists to drive the hostile inputs through it.
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{", encoding="utf-8")
    substitutions = {
        "OUT": str(tmp_path / "out"),
        "OUT.pdf": str(tmp_path / "out.pdf"),
        "OUT.json": str(tmp_path / "out.json"),
        "MISSING.csv": str(tmp_path / "missing.csv"),
        "BAD_JSON": str(bad_json),
    }
    result = cli(*[substitutions.get(part, part) for part in argv])
    assert result.code in {EXIT_OK, EXIT_ERROR, EXIT_USAGE}
