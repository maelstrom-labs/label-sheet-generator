"""Path containment, size caps, JSON strictness, atomic writes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from label_sheet_generator.errors import (
    LimitExceeded,
    NotFoundError,
    UnsafePathError,
    ValidationError,
)
from label_sheet_generator.fsio import (
    dumps_json,
    is_safe_id,
    iter_ids,
    loads_json,
    read_bytes,
    read_json,
    resolve_in_root,
    write_atomic,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    inside = tmp_path / "root"
    (inside / "labels").mkdir(parents=True)
    (inside / "labels" / "a.json").write_text('{"ok": true}')
    (tmp_path / "outside.json").write_text('{"secret": true}')
    return inside


def test_a_legitimate_id_resolves(root: Path) -> None:
    assert resolve_in_root(root, "labels/a", suffix=".json").name == "a.json"


@pytest.mark.parametrize(
    "hostile",
    [
        "../outside",
        "../../etc/passwd",
        "/etc/passwd",
        "labels/../../outside",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com/x",
        "..",
        ".",
        ".hidden",
        "labels/.hidden",
        "a\\b",
        "C:/Windows/system32",
        "labels/a\x00.json",
        "",
    ],
)
def test_escapes_are_refused(root: Path, hostile: str) -> None:
    with pytest.raises((UnsafePathError, NotFoundError)):
        resolve_in_root(root, hostile, suffix=".json")


def test_a_symlink_pointing_outside_the_root_is_refused(root: Path, tmp_path: Path) -> None:
    # Containment must be checked *after* resolution, which is the only point
    # at which a symlink's real target is known.
    link = root / "labels" / "escape.json"
    try:
        link.symlink_to(tmp_path / "outside.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError, match="outside"):
        resolve_in_root(root, "labels/escape", suffix=".json")


def test_a_directory_is_not_a_file(root: Path) -> None:
    with pytest.raises(UnsafePathError, match="not a regular file"):
        resolve_in_root(root, "labels", must_exist=True)


def test_is_safe_id_bounds_the_length() -> None:
    assert is_safe_id("a" * 255)
    assert not is_safe_id("a" * 256)


def test_iter_ids_lists_only_safe_relative_ids(root: Path) -> None:
    assert iter_ids(root) == ["labels/a"]


def test_iter_ids_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert iter_ids(tmp_path / "nope") == []


def test_read_bytes_enforces_the_cap(root: Path) -> None:
    target = root / "labels" / "a.json"
    with pytest.raises(LimitExceeded) as excinfo:
        read_bytes(target, max_bytes=2, what="template")
    assert excinfo.value.limit == 2


def test_json_rejects_the_nan_and_infinity_extensions() -> None:
    # Python's json accepts these by default. They are not JSON, and a NaN
    # reaching the geometry layer defeats every bounds check.
    for hostile in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
        with pytest.raises(ValidationError):
            loads_json(hostile)


def test_json_reports_a_syntax_error_rather_than_raising_valueerror() -> None:
    with pytest.raises(ValidationError, match="not valid JSON"):
        loads_json("{nope}")


def test_json_strips_a_utf8_bom() -> None:
    assert loads_json('\ufeff{"a": 1}'.encode()) == {"a": 1}


def test_json_rejects_undecodable_bytes() -> None:
    with pytest.raises(ValidationError, match="UTF-8"):
        loads_json(b"\xff\xfe\x00bad")


def test_read_json_applies_the_cap(root: Path) -> None:
    assert read_json(root / "labels" / "a.json", max_bytes=1024) == {"ok": True}


def test_write_atomic_replaces_without_a_partial_window(tmp_path: Path) -> None:
    target = tmp_path / "out" / "file.json"
    write_atomic(target, dumps_json({"a": 1}))
    assert json.loads(target.read_text()) == {"a": 1}

    write_atomic(target, dumps_json({"a": 2}))
    assert json.loads(target.read_text()) == {"a": 2}
    # No temp files left behind on the happy path.
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]


def test_write_atomic_cleans_up_when_the_payload_cannot_be_written(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    with pytest.raises(TypeError):
        write_atomic(target, 12345)  # type: ignore[arg-type]
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_dumps_json_is_stable_and_newline_terminated() -> None:
    text = dumps_json({"b": 1, "a": 2})
    assert text.endswith("\n")
    assert text == dumps_json({"b": 1, "a": 2})


def test_dumps_json_refuses_nan() -> None:
    with pytest.raises(ValueError):
        dumps_json({"a": float("nan")})


def test_write_atomic_survives_a_readonly_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        target = tmp_path / "f.json"
        write_atomic(target, "{}")
        assert target.read_text() == "{}"
    finally:
        os.umask(previous)
