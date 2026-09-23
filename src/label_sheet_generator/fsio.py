"""The only module that opens files.

Everything filesystem-shaped flows through :func:`resolve_in_root`. That is the
point: containment is one function with one set of tests, rather than a rule
each call site is expected to remember.

The previous code resolved template names by probing several directories
relative to the process working directory, returning absolute paths verbatim,
falling back to a recursive glob, and finally returning the unresolved path if
nothing matched. That is four separate ways for a name to escape. Here a name
is an *identifier*, not a path: it must match :data:`SAFE_ID_RE`, it is joined
to a root, and the result must still be inside that root after resolution or
it is rejected.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from label_sheet_generator.errors import (
    LimitExceeded,
    NotFoundError,
    UnsafePathError,
    ValidationError,
)

#: A template or asset identifier. Forward slashes are allowed so the catalog
#: can namespace ``labels/basic-address``, but each segment must be a plain
#: name: no ``.``-prefixed segments, no ``..``, no backslashes, no drive
#: letters, no URL schemes.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")

#: Bound on an identifier. Comfortably longer than any real template name and
#: short enough to stay under every filesystem's per-path limit once joined.
MAX_ID_LENGTH = 255

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def is_safe_id(candidate: str) -> bool:
    """True if ``candidate`` is a syntactically valid, contained identifier."""
    return bool(candidate) and len(candidate) <= MAX_ID_LENGTH and bool(SAFE_ID_RE.match(candidate))


def resolve_in_root(
    root: Path,
    identifier: str,
    *,
    suffix: str | None = None,
    must_exist: bool = True,
    kind: str = "file",
) -> Path:
    """Resolve ``identifier`` to a real path strictly inside ``root``.

    The checks run in this order, and each one exists because of a distinct
    escape: reject URL-ish input before it looks like a path; reject anything
    that is not a plain identifier; resolve symlinks; confirm containment
    *after* resolution, since that is the only point at which a symlink's
    target is known; then confirm it is a regular file.

    Raises:
        UnsafePathError: if the identifier is malformed or escapes ``root``.
        NotFoundError: if ``must_exist`` and nothing is there.
    """
    if _SCHEME_RE.match(identifier) or "://" in identifier:
        raise UnsafePathError(
            f"{kind} reference must be a plain name, not a URL",
            loc=(identifier,),
        )
    if not is_safe_id(identifier):
        raise UnsafePathError(
            f"{kind} name {identifier!r} is not valid; use letters, digits, "
            "hyphens, underscores and forward slashes only",
        )

    resolved_root = root.resolve()
    candidate = resolved_root / identifier
    if suffix and candidate.suffix != suffix:
        candidate = candidate.with_name(candidate.name + suffix)

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise UnsafePathError(f"{kind} {identifier!r} could not be resolved") from exc

    if not resolved.is_relative_to(resolved_root):
        raise UnsafePathError(f"{kind} {identifier!r} resolves outside the allowed directory")

    if must_exist:
        if not resolved.exists():
            raise NotFoundError(f"{kind} {identifier!r} was not found")
        if not resolved.is_file():
            raise UnsafePathError(f"{kind} {identifier!r} is not a regular file")

    return resolved


def iter_ids(root: Path, *, suffix: str = ".json") -> list[str]:
    """List identifiers under ``root``, skipping anything unsafe or hidden."""
    if not root.exists():
        return []

    resolved_root = root.resolve()
    ids: list[str] = []
    for path in sorted(resolved_root.rglob(f"*{suffix}")):
        try:
            if not path.resolve().is_relative_to(resolved_root) or not path.is_file():
                continue
        except (OSError, RuntimeError):
            continue
        relative = path.relative_to(resolved_root).with_suffix("").as_posix()
        if is_safe_id(relative):
            ids.append(relative)
    return ids


def read_bytes(path: Path, *, max_bytes: int, what: str = "file") -> bytes:
    """Read a file, refusing to allocate more than ``max_bytes``.

    Size is checked via ``stat`` first so an oversized file is rejected without
    being read, then the read itself is capped in case the file grew between
    the two calls.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise NotFoundError(f"{what} could not be read") from exc

    if size > max_bytes:
        raise LimitExceeded(
            f"{what} is {size} bytes; the maximum is {max_bytes}",
            limit_name="max_bytes",
            limit=max_bytes,
            actual=size,
        )

    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise LimitExceeded(
            f"{what} exceeds the {max_bytes} byte maximum",
            limit_name="max_bytes",
            limit=max_bytes,
            actual=len(data),
        )
    return data


def _reject_constant(constant: str) -> Any:
    raise ValueError(f"{constant} is not valid JSON")


def loads_json(text: str | bytes, *, what: str = "document") -> Any:
    """Parse JSON, rejecting the ``NaN``/``Infinity`` extensions.

    Python's ``json`` accepts those literals by default. They are not JSON, and
    a NaN reaching the geometry layer defeats every bounds check, so they are
    refused at the door.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{what} must be UTF-8 text") from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise ValidationError(f"{what} is not valid JSON: {exc}") from exc


def read_json(path: Path, *, max_bytes: int, what: str = "document") -> Any:
    """Read and parse a JSON file with a size cap."""
    return loads_json(read_bytes(path, max_bytes=max_bytes, what=what), what=what)


def write_atomic(path: Path, data: bytes | str) -> None:
    """Write a file atomically: temp file in the same directory, then replace.

    Writing in place leaves a truncated file if the process dies mid-write, and
    a reader can observe the partial state. The temp file is created in the
    destination directory specifically so ``os.replace`` stays within one
    filesystem, where it is atomic; across devices it would raise ``EXDEV``.
    """
    if not isinstance(data, (str, bytes)):
        raise TypeError(f"write_atomic expects str or bytes, got {type(data).__name__}")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, raw_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(raw_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def dumps_json(payload: Any) -> str:
    """Serialise to stable, human-diffable JSON with a trailing newline."""
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
