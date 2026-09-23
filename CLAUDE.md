# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A label-sheet PDF generator: JSON templates + record data in, print-ready PDF out.
Three surfaces over one engine — a library, a CLI (`label-sheet`), and a FastAPI web
app that serves its own vanilla HTML/CSS/JS frontend from the same process.

Python >= 3.10. Package lives in `src/label_sheet_generator/`.

## Commands

```bash
pip install -e '.[dev,web,pdfimport]'   # full dev environment

pytest                                   # 818 tests, must stay green
pytest tests/test_security.py            # the regression suite for past CVEs-in-spirit
ruff check src tests && ruff format src tests
mypy                                     # strict, and it passes - keep it that way
python .github/scripts/check_frontend.py # XSS/CDN gate on the frontend

label-sheet serve --port 8000            # the web app
label-sheet list-templates               # see the catalog
```

CI runs all of the above across 3.10-3.13, plus a wheel install into a clean venv and a
container build. Coverage gate is 80% (currently ~87%).

## Architecture

Dependency direction is strictly one way. Do not add an import that points backwards.

```
api/  ->  service  ->  {catalog, records, render/, schema}  ->  {fsio, geometry, units, errors, settings}
```

- **Nothing below `api/` imports FastAPI.** The CLI and library must work without the
  `[web]` extra installed.
- **Only `fsio.py` opens files.** Everything path-shaped goes through
  `fsio.resolve_in_root()`. There is exactly one containment check in this codebase and
  that is deliberate.
- `service.py` is the only thing `api/` calls. It owns work limits and the render pool.
- `schema.py` is the trust boundary: anything that has been through it is valid, finite,
  positively-sized and free of unknown keys, so nothing downstream re-checks.

| Module | Responsibility |
|---|---|
| `errors.py` | The domain exception hierarchy. Every failure is one of these. |
| `units.py` | mm/in/pt conversion and the single rounding policy (`quantize`, 6dp). |
| `schema.py` | Pydantic v2 models for templates, elements, records. |
| `geometry.py` | Slot positions and `validate()`. Pure arithmetic, no I/O. |
| `fsio.py` | The only filesystem module. Sandboxing, size caps, atomic writes. |
| `settings.py` | All roots and limits, resolved once from `LSG_*` env vars. |
| `catalog.py` | Template index, built once at startup. Also the legacy-format loader. |
| `records.py` | Record document + CSV/TSV parsing. No I/O, no rendering. |
| `render/` | `pdf.py` orchestrates; `text/barcode/image.py` draw; `preview.py` rasterises. |
| `assets.py` | Sandboxed image loading. The only path to `ImageReader`. |
| `service.py` | Composes everything; enforces limits, concurrency, timeouts. |
| `api/` | HTTP only. Routes, middleware, error mapping. |
| `static/` | The frontend. Hand-written, no build step. |

## Invariants

These encode bugs that were found and fixed. Breaking one reintroduces a real defect, and
each has a regression test in `tests/test_security.py` or `tests/test_review_regressions.py`.

1. **No bare builtin exception may escape a public function.** `KeyError`, `TypeError`,
   `ValueError`, `OSError` from user input all become HTTP 500. Catch and re-raise as
   something from `errors.py`. No user input may produce a 5xx — that is tested
   exhaustively.
2. **Never `str.format` / `format_map` on a user-supplied string.** It walks attributes
   (`{x.__class__.__mro__}`) and allocates unbounded padding (`{x:>999999999}`). Use
   `schema.render_template_string`.
3. **Images only via `AssetLoader`.** Never hand `ImageReader` a path or URL — always
   `BytesIO` of bytes you read yourself from inside `ASSET_ROOT`. Record data is user
   input; this was a live arbitrary-file-read.
4. **Pydantic models: `extra='forbid'`, `allow_inf_nan=False`, frozen.** Element type is a
   `Literal` discriminator, never an assigned mutable field — `@dataclass(slots=True)` plus
   zero-arg `super()` is what broke serialization for every template before the rebuild.
5. **NaN and infinity die at the boundary.** Every comparison against NaN is `False`, so it
   sails through bounds checks and surfaces much later as a 500. `units.check_finite` and
   `fsio.loads_json` reject them.
6. **All four margins are enforced.** `margin_right_mm` and `margin_bottom_mm` were once
   parsed and never read.
7. **Every limit is a named constant** surfaced by name in the 4xx it produces, and listed
   in `settings.public_limits()` so the frontend can pre-validate.
8. **The frontend never assigns HTML.** No `innerHTML`, `outerHTML`,
   `insertAdjacentHTML`, `eval`, `new Function`, inline handlers, or CDN references.
   Build DOM with `createElement`/`textContent`. CI greps for this.
9. **No temp files on the HTTP path.** Everything is `BytesIO` end to end. `fsio.write_atomic`
   is for the CLI.
10. **Backward compatibility with on-disk formats.** Existing `templates/*.json` and record
    files must keep parsing, including `_mm`/`_in` dual keys, `template_type`-less
    documents, and bare top-level record arrays. A template authored in inches round-trips
    as inches.

## Library gotchas

Each cost real debugging time:

- **ReportLab swaps the MediaBox** when `/Rotate` is 90 or 270, but leaves the content
  stream in the original coordinate space — two rows of a sheet silently fall off the page.
  `render/pdf.py::_pin_media_boxes` pins it. Do not remove that.
- **`Future.cancel()` returns False once a task is running.** CPython cannot interrupt a
  thread mid-render, so a timed-out render keeps its worker. The concurrency slot transfers
  to the abandoned task and is released by a done-callback, not by the waiter.
- **`concurrent.futures.TimeoutError` is only an alias of the builtin from 3.11.** On 3.10
  catching the builtin alone lets it escape. Catch `FuturesTimeoutError` explicitly.
- **Starlette echoes the request `Origin`** when a CORS wildcard is combined with
  credentials, so a wildcard is not a relaxation, it is an any-origin policy. `Settings`
  refuses `*` at startup.
- **`X-Forwarded-For` can arrive as several header lines** (RFC 7230). Join them all;
  reading only the first is a rate-limiter bypass.
- **`json` accepts `NaN`/`Infinity` by default.** They are not JSON.
- **Keep header/record dedup linear.** Restarting a per-name counter inside a loop made CSV
  import quadratic: a 64KB upload cost 63s of CPU.

## Style

Match the existing modules — they are the reference.

- `from __future__ import annotations` at the top of every module.
- Full type hints on every function. Module and public-symbol docstrings.
- **Comments explain WHY, never WHAT.** Do not narrate the code. A comment earns its place
  by recording a decision, a constraint, or a defect it prevents.
- No magic numbers: a module-level constant with a comment saying why that value.
- Plain ASCII. No emoji anywhere, including commit messages.
- Prefer a small named function and an early return over a nested branch.

## Testing

- One behaviour per test; the name states the expected behaviour.
- A comment on a test only when it encodes a past defect — say what broke.
- `parametrize` for families of input.
- Assert on behaviour, not implementation. Match error messages on a substring unless the
  wording is the contract.
- New security-relevant behaviour needs a test in `tests/test_security.py`.

## Things that are intentional

Do not "fix" these:

- Image elements are **disabled** unless `LSG_ASSET_DIR` is set. Safe default for a public
  instance, not an oversight.
- CORS middleware is **absent** by default. The SPA is same-origin.
- PDF import is **off** on the server by default (`LSG_ENABLE_PDF_IMPORT`) — parsing
  untrusted PDFs is a much larger attack surface than JSON.
- `/api/docs` carries its own relaxed CSP because Swagger loads from a CDN. The app's own
  pages keep the strict policy; `/api/openapi.json` works offline.
- The Avery presets are declared in **exact published inches** and derived to mm, with an
  axis-closure assertion at import. Do not replace them with rounded mm literals.
