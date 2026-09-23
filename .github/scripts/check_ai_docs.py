"""Keep the three AI instruction files honest and in step.

There are three of them -- CLAUDE.md, .github/copilot-instructions.md and the
Ollama Modelfile -- because each tool loads a different path. Three copies drift,
and a drifted instruction file is worse than none: the version of CLAUDE.md this
check replaced still described `WorkspaceConfig` and the Streamlit UI long after
both were deleted, so every assistant that read it started from a false picture.

Two failure modes, two checks:

* **Staleness** -- a file names a module or symbol that no longer exists. Caught
  by resolving every ``module.attr`` and ``module.py`` reference against the
  installed package.
* **Drift** -- a rule is added to one file and forgotten in the others. Caught by
  requiring every entry in SHARED below to appear in all three, matched loosely
  enough that each file can keep its own wording.

Run: python .github/scripts/check_ai_docs.py
"""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "label_sheet_generator"
SOURCE = ROOT / "src" / PACKAGE

DOCS = {
    "CLAUDE.md": ROOT / "CLAUDE.md",
    "copilot": ROOT / ".github" / "copilot-instructions.md",
    "Modelfile": ROOT / "Modelfile",
}


@dataclass(frozen=True)
class Shared:
    """A rule that must be stated in every instruction file.

    ``patterns`` are alternatives: one match is enough. They are deliberately
    loose, because the three files say the same thing at three different
    lengths and forcing identical prose would just produce three identical
    files with no reason to exist separately.
    """

    name: str
    patterns: tuple[str, ...]

    def missing_from(self, text: str) -> bool:
        return not any(re.search(p, text, re.IGNORECASE | re.DOTALL) for p in self.patterns)


SHARED: tuple[Shared, ...] = (
    Shared("layering direction", (r"api/?\s*->\s*service\s*->",)),
    Shared(
        "no FastAPI below api/",
        (r"(never|not) import fastapi below", r"nothing below `?api/?`? imports fastapi"),
    ),
    Shared("filesystem only via fsio", (r"fsio\.resolve_in_root",)),
    Shared(
        "no bare builtin exceptions escape",
        (r"bare\s+builtin\s+exception", r"bare\s+`?keyerror"),
    ),
    Shared("no str.format on user strings", (r"str\.format",)),
    Shared("no path/URL to ImageReader", (r"imagereader",)),
    Shared("pydantic extra=forbid / frozen", (r"extra\s*=\s*[\"']?forbid",)),
    Shared("reject NaN and infinity", (r"nan and infinit|reject nan",)),
    Shared("limits are named constants", (r"limit is a named constant",)),
    Shared("frontend never assigns HTML", (r"innerhtml",)),
    Shared("no temp files on the HTTP path", (r"no temp files on the http path",)),
    Shared("keep loops linear", (r"quadratic",)),
    Shared("on-disk formats are compatible", (r"_mm and _in|`_mm`/`_in`|_mm`? and `?_in",)),
    Shared("comments explain why", (r"explain\s+\*?\*?why",)),
    Shared("no emoji", (r"no emoji",)),
    Shared("future annotations", (r"from __future__ import annotations",)),
    Shared("pytest", (r"\bpytest\b",)),
    Shared("ruff", (r"\bruff\b",)),
    Shared("mypy", (r"\bmypy\b",)),
    Shared("frontend gate script", (r"check_frontend\.py",)),
    Shared("these files must stay in step", (r"check_ai_docs\.py",)),
)

#: Referenced files that must exist, relative to the repo root.
REFERENCED_PATHS = (
    ".github/scripts/check_frontend.py",
    "src/label_sheet_generator/static/app.js",
    "tests/test_security.py",
)

#: ``module.attr`` references are only resolved for real package modules, so
#: prose like ``str.format`` or ``page.rotate`` is not mistaken for one.
MODULES = {path.stem for path in SOURCE.glob("*.py")} - {"__init__", "__main__"}

#: ``py`` is excluded so a file reference like `errors.py` is not read as an
#: attribute lookup; check_module_files handles those.
_SYMBOL_RE = re.compile(r"`([a-z_][a-z0-9_]*)\.(?!py`)([A-Za-z_][A-Za-z0-9_]*)`")
_FILE_RE = re.compile(r"`([a-z_][a-z0-9_]*)\.py`")


def check_symbols(label: str, text: str) -> list[str]:
    """Every `module.attr` the file promises must actually import."""
    problems = []
    for module_name, attribute in sorted(set(_SYMBOL_RE.findall(text))):
        if module_name not in MODULES:
            continue
        try:
            module = importlib.import_module(f"{PACKAGE}.{module_name}")
        except ImportError as exc:  # pragma: no cover - a broken install
            problems.append(f"{label}: cannot import {module_name} ({exc})")
            continue
        if not hasattr(module, attribute):
            problems.append(
                f"{label}: references `{module_name}.{attribute}`, which no longer exists"
            )
    return problems


#: Where a `something.py` reference is allowed to live. Package modules first,
#: then the repo's own scripts and tests, which the files also cite by name.
_PY_SEARCH_DIRS = (
    SOURCE,
    SOURCE / "render",
    SOURCE / "api",
    ROOT / ".github" / "scripts",
    ROOT / "tests",
)


def check_module_files(label: str, text: str) -> list[str]:
    """Every `something.py` named must be a real file somewhere sensible."""
    problems = []
    for stem in sorted(set(_FILE_RE.findall(text))):
        if any((directory / f"{stem}.py").is_file() for directory in _PY_SEARCH_DIRS):
            continue
        problems.append(f"{label}: references `{stem}.py`, which does not exist")
    return problems


def check_shared(texts: dict[str, str]) -> list[str]:
    """Every shared rule must be stated in all three files."""
    problems = []
    for rule in SHARED:
        absent = [label for label, text in texts.items() if rule.missing_from(text)]
        if absent:
            problems.append(f"{', '.join(absent)}: missing the shared rule '{rule.name}'")
    return problems


def main() -> int:
    problems: list[str] = []
    texts: dict[str, str] = {}

    for label, path in DOCS.items():
        if not path.is_file():
            problems.append(f"{label}: {path.relative_to(ROOT)} is missing")
            continue
        texts[label] = path.read_text(encoding="utf-8")

    if len(texts) == len(DOCS):
        problems += check_shared(texts)
        for label, text in texts.items():
            problems += check_symbols(label, text)
            problems += check_module_files(label, text)

    for relative in REFERENCED_PATHS:
        if not (ROOT / relative).exists():
            problems.append(f"the instruction files reference {relative}, which is missing")

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(
            f"\n{len(problems)} problem(s). The AI instruction files are the first thing "
            "an assistant reads; a stale one is worse than none."
        )
        return 1

    print(
        f"AI instruction files agree on {len(SHARED)} shared rules and every symbol "
        "they name exists."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
