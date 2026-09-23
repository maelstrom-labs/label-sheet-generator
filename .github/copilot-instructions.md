# GitHub Copilot instructions

Label-sheet PDF generator: JSON templates + record data in, print-ready PDF out.
A library, a CLI (`label-sheet`), and a FastAPI app serving a vanilla HTML/CSS/JS
frontend from the same process. Python >= 3.10, package in `src/label_sheet_generator/`.

## Code style

- Start every module with `from __future__ import annotations`.
- Full type hints on every function and method. Docstrings on modules and public symbols.
- Comments explain **why**, never what. Do not narrate code.
- No magic numbers — use a module-level constant with a comment justifying the value.
- Plain ASCII only. No emoji, including in commit messages.
- Prefer small named functions and early returns to nested branches.
- `ruff` (line length 100) and `mypy --strict` both pass; keep them passing.

## Layering

Imports point one way only:

```
api/ -> service -> {catalog, records, render/, schema} -> {fsio, geometry, units, errors, settings}
```

- Never import FastAPI below `api/`. The CLI and library must work without the `[web]` extra.
- Never open a file outside `fsio.py`. Use `fsio.resolve_in_root()` for anything path-shaped.
- `api/` calls only `service.py`.
- `schema.py` is the validation boundary — do not re-validate downstream of it.

## Rules that prevent known defects

Each of these has a regression test. Suggest code that honours them.

- **Never raise a bare `KeyError`/`TypeError`/`ValueError`/`OSError` out of a public
  function.** Convert to a type from `errors.py`. Anything else becomes an HTTP 500, and
  no user input is allowed to produce a 5xx.
- **Never call `str.format` or `format_map` on a user-supplied string.** Use
  `schema.render_template_string`. The format mini-language walks attributes and allocates
  unbounded padding.
- **Never pass a path or URL to `ImageReader`.** Load bytes through `AssetLoader` and hand
  it a `BytesIO`. Image references in records are user input.
- **Pydantic models** use `extra="forbid"`, `allow_inf_nan=False`, `frozen=True`. Element
  subtype is a `Literal` discriminator, never an assigned field.
- **Reject NaN and infinity at the boundary** (`units.check_finite`, `fsio.loads_json`).
  Bounds checks silently pass for NaN.
- **Every limit is a named constant**, reported by name in the error it raises.
- **Frontend (`static/app.js`): never `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `eval`, `new Function`, inline `on*=` handlers, or any CDN URL.** Build DOM with
  `createElement` and `textContent`; template names, field names, record values and server
  error strings are all untrusted. CI fails the build on a violation.
- **No temp files on the HTTP path** — `BytesIO` end to end.
- **Keep per-item loops linear.** Restarting a counter inside a loop once made CSV header
  dedup quadratic (63s of CPU for a 64KB upload).

## Compatibility

On-disk formats are public API. Existing template and record files must keep parsing:
`_mm` and `_in` dual keys, documents with no `template_type`, and bare top-level record
arrays. A template authored in inches must round-trip as inches.

## Tests

`pytest`, in `tests/`. One behaviour per test, named for the expected behaviour;
`parametrize` for families of input; assert on behaviour rather than implementation.
Comment a test only to record the past defect it pins. Security-relevant changes need a
case in `tests/test_security.py`.

## Commands

```bash
pytest
ruff check src tests && ruff format src tests
mypy
python .github/scripts/check_frontend.py
label-sheet serve --port 8000
```
