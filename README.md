# Label Sheet Generator

Generate print-ready label sheet PDFs from JSON templates and record data — from a web
interface, a CLI, or as a Python library.

Define a grid in millimetres or inches, drop in your records, and export a PDF that lines up
with the label stock in your printer. Avery presets, Code 128 / EAN-13 / QR barcodes, images,
and CSV import are built in.

- **Self-hostable for free.** One container, one process, no database, no Node build step.
- **Same engine everywhere.** The web app, the CLI and the library share one rendering path,
  so a preview in the browser is the PDF you get on disk.
- **Backward compatible.** The JSON template and record formats are unchanged from 0.x.

## Quick start

```bash
pip install 'label-sheet-generator[web]'
label-sheet serve          # http://127.0.0.1:8000
```

Or without the web extras, as a CLI:

```bash
pip install label-sheet-generator
label-sheet list-templates
label-sheet generate labels/basic-address out.pdf --records records.json
```

## Deploy it

The image honours `$PORT`, runs as a non-root user, and exposes `/api/livez` and
`/api/readyz` for platform probes.

```bash
docker build -t label-sheet-generator .
docker run --rm -p 8000:8000 label-sheet-generator
```

| Platform | How |
|---|---|
| **Render** | Free web service. `render.yaml` is committed; point Render at the repo. |
| **Fly.io** | `fly launch --no-deploy && fly deploy`. `fly.toml` is committed and scales to zero. |
| **Koyeb** | Free web service from the Dockerfile; set the port to 8000. |
| **Hugging Face Spaces** | Docker Space; add `app_port: 8000` to the Space README front matter. |
| **Railway / Cloud Run** | Deploy from the Dockerfile; both inject `$PORT`. |

Behind a TLS-terminating proxy, set `LSG_TRUSTED_PROXY_HOPS=1` so rate limiting keys on the
real client address rather than the proxy's.

## Configuration

Everything is environment variables; every one has a safe default. There is no config file.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` / `LSG_PORT` | `8000` | Listen port |
| `LSG_HOST` | `127.0.0.1` | Bind address (the image sets `0.0.0.0`) |
| `LSG_TEMPLATE_DIR` | — | Extra templates layered over the built-ins |
| `LSG_ASSET_DIR` | — | **Required to enable image elements.** The only directory images may load from |
| `LSG_CORS_ORIGINS` | — | Comma-separated explicit origins. `*` is refused |
| `LSG_MAX_RECORDS` | `5000` | Records per render |
| `LSG_MAX_PAGES` | `200` | Pages per render |
| `LSG_MAX_BODY_BYTES` | `2097152` | Request body cap |
| `LSG_MAX_UPLOAD_BYTES` | `2097152` | Upload cap |
| `LSG_MAX_CONCURRENT_RENDERS` | `4` | Render worker pool size |
| `LSG_RENDER_TIMEOUT_S` | `20` | Per-render wall-clock budget |
| `LSG_TRUSTED_PROXY_HOPS` | `0` | Proxies in front; `X-Forwarded-For` is ignored at `0` |
| `LSG_ENABLE_PDF_IMPORT` | `false` | PDF template import (parses untrusted PDFs) |
| `LSG_LOG_LEVEL` | `INFO` | Log level; output is JSON |

**Image elements are disabled unless `LSG_ASSET_DIR` is set.** Record data is user input, so
an unrestricted image path is a file-read primitive. With the variable set, references are
resolved strictly inside that directory; absolute paths, `..`, and URLs are refused.

## CLI

```bash
label-sheet list-templates                      # built-ins, your templates, Avery presets
label-sheet check labels/basic-address          # validate geometry without rendering
label-sheet generate <template> out.pdf \
    --records data.csv \                        # CSV or JSON
    --layout layouts/basic-address \            # optional: swap in a different arrangement
    --orientation landscape \
    --margin-top 12.7 --margin-left 4.76 \
    --borders                                   # guide boxes for alignment on plain paper
label-sheet avery-template 5160 my-sheet.json   # start from an Avery product code
label-sheet import-template sheet.pdf out.json  # measure a grid from a PDF (see below)
label-sheet serve --port 8000                   # the web interface
```

Exit codes: `0` success, `1` failure, `2` invalid input or usage.

### Importing a template from a PDF

If you have a vendor's PDF for a label sheet, the importer measures the grid from its
vector outlines rather than making you transcribe a datasheet:

```bash
label-sheet import-template avery-5160.pdf my-sheet.json
# detected a 3x10 grid from the page rectangles (confidence 0.99)
```

It reports which method it used (`rectangles`, `curves`, or `lines`) and a confidence
score. Below 0.7 it warns you to print one sheet and check it against the physical stock
before committing to a full run. When detection cannot measure the grid it says which
flags it needs rather than guessing:

```bash
label-sheet import-template sheet.pdf out.json \
    --rows 10 --cols 3 --label-width-mm 66.675 --label-height-mm 25.4 \
    --margin-left-mm 4.7625 --margin-top-mm 12.7 --gap-x-mm 3.175
label-sheet import-template sheet.pdf out.json --template-code 5160   # or borrow a preset
```

A detection that fails a sanity check — a gap wider than its label, a grid that does not
fit the page, the two parsers disagreeing on page size — is discarded with an explanation
rather than written out as a plausible-looking but wrong template.

Import needs an extra, because parsing untrusted PDFs is a much larger attack surface
than reading JSON:

```bash
pip install 'label-sheet-generator[pdfimport]'
```

It is a CLI feature. On a server it stays off unless you set `LSG_ENABLE_PDF_IMPORT=true`.

## Template format

Any geometric value may use a `_mm` or `_in` suffix. The renderer normalises to millimetres
internally, and writes files back in the unit they were authored in.

```json
{
  "name": "shipping-labels",
  "page": { "width_mm": 215.9, "height_mm": 279.4 },
  "grid": {
    "rows": 10, "cols": 3,
    "margin_left_mm": 4.7625, "margin_top_mm": 12.7,
    "margin_right_mm": 4.7625, "margin_bottom_mm": 12.7,
    "gap_x_mm": 3.175, "gap_y_mm": 0,
    "label_width_mm": 66.675, "label_height_mm": 25.4
  },
  "elements": [
    { "type": "text", "field": "name", "x_mm": 4, "y_mm": 3,
      "width_mm": 58, "height_mm": 8,
      "font_name": "Helvetica-Bold", "font_size_pt": 11 },
    { "type": "text", "template": "{address_1}\n{address_2}",
      "x_mm": 4, "y_mm": 12, "width_mm": 58, "height_mm": 8, "font_size_pt": 8 },
    { "type": "barcode", "field": "sku", "barcode_type": "code128",
      "x_mm": 4, "y_mm": 20, "width_mm": 50, "height_mm": 10 },
    { "type": "image", "field": "logo", "x_mm": 54, "y_mm": 2,
      "width_mm": 10, "height_mm": 10, "fit": "contain" }
  ]
}
```

Element coordinates are measured from the top-left of each label.

| Element | Options |
|---|---|
| `text` | `font_name`, `font_size_pt`, `leading_pt`, `color`, `align` (`left`/`center`/`right`/`justify`), `valign` (`top`/`middle`/`bottom`), `rotation_deg`, `overflow` (`shrink`/`clip`/`truncate`/`error`) |
| `barcode` | `barcode_type` (`code128`/`ean13`/`qr`), `human_readable`, `quiet_zone` |
| `image` | `fit` (`contain`/`cover`/`stretch`), `align`, `valign` |

Content comes from exactly one of `field` (a record key), `template` (a `{field}` string), or
`value` (a literal). `{field}` references are plain names only — attribute access, indexing
and format specs are rejected.

### Text layout templates

A layout reuses a label template's page and grid but supplies its own elements, so one sheet
geometry can drive several designs:

```json
{
  "template_type": "text-layout",
  "name": "address-layout",
  "elements": [ { "type": "text", "field": "name", "x_mm": 4, "y_mm": 3,
                  "width_mm": 58, "height_mm": 8, "align": "center" } ]
}
```

## Record format

```json
{
  "schema": ["name", "address_1", "sku"],
  "records": [
    { "name": "Ada Lovelace", "address_1": "12 Analytical Engine Way", "sku": "AL-1001" }
  ]
}
```

A bare top-level array is also accepted and normalised. CSV and TSV import needs a header
row; the delimiter is sniffed, duplicate headers are suffixed, and ragged rows are padded
with a warning rather than dropped.

## Library

```python
from label_sheet_generator.render import RenderOptions, render_pdf
from label_sheet_generator.schema import LabelTemplate

template = LabelTemplate.model_validate(json.loads(Path("sheet.json").read_text()))
result = render_pdf(template, records, options=RenderOptions(outline_slots=True))
Path("out.pdf").write_bytes(result.pdf_bytes)
print(result.page_count, result.label_count, result.warnings)
```

`LabelTemplate` is a Pydantic model: invalid geometry raises at construction rather than
producing a broken PDF. `label_sheet_generator.geometry.validate` reports problems without
rendering.

## HTTP API

Interactive docs at `/api/docs`. Errors are uniform:

```json
{ "error": { "code": "validation_error", "message": "...", "request_id": "…", "details": [] } }
```

| Endpoint | Purpose |
|---|---|
| `GET /api/livez` `GET /api/readyz` `GET /api/version` | Probes |
| `GET /api/bootstrap` | Templates, layouts, fonts, limits — one call |
| `GET /api/templates/{id}` | A normalised template |
| `POST /api/templates/validate` | Validate a template document |
| `POST /api/records/parse` | CSV/JSON upload → canonical document |
| `POST /api/render/plan` | Counts and diagnostics, no PDF |
| `POST /api/render/preview` | One page as PNG |
| `POST /api/render/pdf` | The sheet |
| `GET /api/presets/avery` | Built-in Avery presets |

## Security

The web app is written to be exposed to the internet:

- Image references are confined to `LSG_ASSET_DIR`; absolute paths, `..` and URLs are refused,
  and image elements are off entirely unless that directory is configured.
- `{field}` substitution uses a restricted grammar, never `str.format`.
- CORS is absent by default; `*` is refused at startup.
- CSP with no `unsafe-inline`, plus `nosniff`, `no-referrer`, `frame-ancestors 'none'`.
- Request body, upload, record count, page count, output size and wall-clock budgets, plus a
  per-IP token bucket and a bounded render pool that sheds with `503` rather than queueing.
- No user input produces a `5xx`; tracebacks are logged, never returned.

Report vulnerabilities via a private GitHub security advisory.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,web,pdfimport]'

pytest                                    # tests
ruff check src tests && ruff format src tests
mypy                                      # strict
python .github/scripts/check_frontend.py  # frontend XSS/CDN gate
label-sheet serve --port 8000
```

### AI assistant instructions

The same contributor guidance — layering rules, the invariants that prevent known
defects, and house style — is kept in three places, one per tool:

| File | Tool |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Claude Code (loaded automatically) |
| [`.github/copilot-instructions.md`](.github/copilot-instructions.md) | GitHub Copilot (loaded automatically) |
| [`Modelfile`](Modelfile) | Ollama — `ollama create label-sheet -f Modelfile` |

`CLAUDE.md` is the fullest version; the other two are condensed from it. If you change
one, change the others.

## License

MIT. See [LICENSE](LICENSE).
