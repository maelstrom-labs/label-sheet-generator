"""Font availability checks.

The old renderer passed ``element.font_name`` straight to reportlab, so an
unregistered name raised a bare ``KeyError`` from deep inside a drawing call
and surfaced as an HTTP 500.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from label_sheet_generator.errors import ConfigurationError, ValidationError
from label_sheet_generator.fonts import (
    STANDARD_FONTS,
    available_fonts,
    ensure_available,
    is_available,
    register_font_dir,
)


def test_the_standard_pdf_fonts_are_always_available() -> None:
    for name in ("Helvetica", "Helvetica-Bold", "Times-Roman", "Courier"):
        assert is_available(name), name
        assert name in available_fonts()


def test_available_fonts_is_sorted_and_deduplicated() -> None:
    fonts = available_fonts()
    assert fonts == sorted(set(fonts))
    assert len(fonts) >= len(STANDARD_FONTS)


def test_ensure_available_returns_a_known_name_unchanged() -> None:
    assert ensure_available("Helvetica") == "Helvetica"


def test_an_unknown_font_raises_a_typed_error_not_a_keyerror() -> None:
    with pytest.raises(ValidationError):
        ensure_available("Comic Sans MS")


def test_the_error_suggests_near_matches() -> None:
    # A typo is the common case, so the message should close the loop rather
    # than just reporting failure.
    with pytest.raises(ValidationError, match="Helvetica"):
        ensure_available("Helvetcia")


def test_the_error_carries_the_location_it_was_given() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ensure_available("Nope", loc=("elements", 2, "font_name"))
    assert excinfo.value.loc == ("elements", 2, "font_name")


def test_case_matters_because_reportlab_lookups_are_exact() -> None:
    assert not is_available("helvetica")


def test_registering_a_directory_of_nothing_adds_nothing(tmp_path: Path) -> None:
    assert register_font_dir(tmp_path) == []


def test_a_corrupt_font_file_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    # One unreadable file in a font directory must not stop the server booting.
    (tmp_path / "broken.ttf").write_bytes(b"this is not a font")
    (tmp_path / "also-broken.otf").write_bytes(b"\x00\x01\x02")
    assert register_font_dir(tmp_path) == []
    assert is_available("Helvetica")


def test_a_path_that_is_not_a_directory_is_a_configuration_error(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(ConfigurationError):
        register_font_dir(target)


def test_a_missing_directory_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        register_font_dir(tmp_path / "nope")
