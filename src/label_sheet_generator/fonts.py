"""The font registry, and the one place a font name is validated.

The previous implementation passed ``element.font_name`` straight through to
``canvas.setFont``. An unregistered name surfaced as a bare ``KeyError`` from
inside reportlab's metrics tables, several frames below anything that knew the
request existed, and became an HTTP 500 on what is plainly a 422: the client
asked for a font that does not exist.

So the rule is that a font name is *validated at the edge*, by
:func:`ensure_available`, and the renderer only ever calls ``setFont`` with a
name this module has already vouched for. The failure carries near matches,
because the overwhelmingly common cause is a spelling or casing slip
(``"helvetica"``, ``"Helvitica"``, ``"Arial"``) rather than a genuinely absent
typeface.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from threading import Lock

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from label_sheet_generator.errors import (
    ConfigurationError,
    LabelSheetError,
    Loc,
    ValidationError,
)
from label_sheet_generator.fsio import iter_ids, resolve_in_root

#: The 14 base fonts every conforming PDF consumer provides without embedding.
#: reportlab exposes the same list, and that is preferred so the two can never
#: drift, but it is a private-ish module attribute, so this literal is the
#: fallback if a future release moves it.
_FALLBACK_STANDARD_FONTS: tuple[str, ...] = (
    "Courier",
    "Courier-Bold",
    "Courier-BoldOblique",
    "Courier-Oblique",
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-BoldOblique",
    "Helvetica-Oblique",
    "Symbol",
    "Times-Bold",
    "Times-BoldItalic",
    "Times-Italic",
    "Times-Roman",
    "ZapfDingbats",
)


def _standard_fonts() -> tuple[str, ...]:
    names = getattr(pdfmetrics, "standardFonts", None)
    if not names:
        return _FALLBACK_STANDARD_FONTS
    try:
        return tuple(sorted(str(name) for name in names))
    except TypeError:  # not iterable in some future reportlab
        return _FALLBACK_STANDARD_FONTS


#: Always-available font names, sorted.
STANDARD_FONTS: tuple[str, ...] = _standard_fonts()

#: Font file extensions :func:`register_font_dir` will attempt. Bitmap and
#: PostScript Type 1 formats are excluded: reportlab's TrueType loader cannot
#: read them, so offering them would only produce silent skips.
FONT_SUFFIXES: tuple[str, ...] = (".ttf", ".otf")

#: Suggestions attached to an unknown-font error. Three is enough to cover the
#: casing, spelling and family-member cases without turning the message into a
#: catalogue.
MAX_SUGGESTIONS = 3

#: Similarity floor for a suggestion. 0.5 admits "helvetica" -> "Helvetica"
#: and "Times" -> "Times-Roman" while excluding unrelated names.
SUGGESTION_CUTOFF = 0.5

# reportlab's font registry is process-global mutable state, so concurrent
# registration of the same directory could otherwise interleave.
_LOCK = Lock()

#: Names this module has registered, mapped to the file they came from. Used
#: to make repeated registration a no-op rather than a redundant re-parse.
_REGISTERED: dict[str, Path] = {}


def _registered_names() -> tuple[str, ...]:
    try:
        return tuple(str(name) for name in pdfmetrics.getRegisteredFontNames())
    except Exception:
        # Listing fonts is used by error paths and by /api/bootstrap; a
        # registry that cannot be enumerated must degrade to the base 14
        # rather than mask the original failure with a new one.
        return ()


def available_fonts() -> list[str]:
    """Every font name that may be used, sorted."""
    return sorted(set(STANDARD_FONTS) | set(_registered_names()))


def is_available(name: str) -> bool:
    """True if ``name`` can be passed to ``canvas.setFont`` safely."""
    candidate: object = name
    if not isinstance(candidate, str) or not candidate:
        return False
    return candidate in STANDARD_FONTS or candidate in _registered_names()


def ensure_available(name: str, *, loc: Loc = ()) -> str:
    """Return ``name`` unchanged if it is usable, else explain what to use.

    Args:
        name: The font name taken from a template element.
        loc: Where the name came from, for the structured error payload.

    Raises:
        ValidationError: if the font is not registered. The message names up to
            :data:`MAX_SUGGESTIONS` close matches.
    """
    candidate: object = name
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValidationError("font name must be a non-empty string", loc=loc)

    if is_available(name):
        return name

    known = available_fonts()
    suggestions = difflib.get_close_matches(
        name, known, n=MAX_SUGGESTIONS, cutoff=SUGGESTION_CUTOFF
    )
    if not suggestions:
        suggestions = known[:MAX_SUGGESTIONS]
        hint = "available fonts include"
    else:
        hint = "did you mean"

    raise ValidationError(
        f"font {name!r} is not available; {hint}: {', '.join(suggestions)}",
        loc=loc,
    )


def register_font_dir(path: Path) -> list[str]:
    """Register every usable TrueType font in ``path``.

    A font file is operator-supplied rather than request-supplied, but one
    corrupt or unsupported file in a directory must not take down startup, so
    each file is attempted independently and failures are skipped. Only the
    directory itself being wrong is worth failing on, since that is a typo in
    configuration rather than a bad asset.

    Fonts are named after their path relative to ``path``, without the
    extension: ``Inter-Regular.ttf`` registers as ``Inter-Regular`` and
    ``brand/Inter-Regular.ttf`` as ``brand/Inter-Regular``.

    Args:
        path: A directory of ``.ttf`` or ``.otf`` files.

    Returns:
        The names newly added, sorted. Already-registered fonts are omitted,
        which makes calling this twice a no-op.

    Raises:
        ConfigurationError: if ``path`` is not a directory.
    """
    try:
        is_directory = path.is_dir()
    except OSError as exc:
        raise ConfigurationError(f"font directory {path} could not be read") from exc
    if not is_directory:
        raise ConfigurationError(f"font directory {path} is not a directory")

    added: list[str] = []
    with _LOCK:
        for suffix in FONT_SUFFIXES:
            for identifier in iter_ids(path, suffix=suffix):
                if identifier in _REGISTERED:
                    continue
                font_path = _resolve_font(path, identifier, suffix)
                if font_path is not None and _register_one(identifier, font_path):
                    added.append(identifier)
    return sorted(added)


def _resolve_font(root: Path, identifier: str, suffix: str) -> Path | None:
    """Contain one font path, or None if the name is not safely resolvable."""
    try:
        return resolve_in_root(root, identifier, suffix=suffix, kind="font")
    except LabelSheetError:
        return None


def _register_one(name: str, font_path: Path) -> bool:
    """Attempt one registration. Returns True only if the font is now usable.

    Every failure mode here -- an unsupported CFF-outline OTF, a truncated
    file, a font whose internal tables disagree -- is reported by reportlab as
    one of several unrelated exception types, so the catch is deliberately
    broad. Nothing is logged: a directory of fonts is scanned on every start,
    and a warning per unusable file would be pure noise.
    """
    try:
        pdfmetrics.registerFont(TTFont(name, str(font_path)))
    except Exception:
        return False
    _REGISTERED[name] = font_path
    return True
