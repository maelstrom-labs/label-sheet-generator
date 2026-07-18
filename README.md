# Label Sheet Generator

Generate custom printable label sheets as PDFs from structured JSON input. Define label size, grid layout, margins, spacing, and per-label content. Render text, barcodes, and images with a simple Python CLI and reusable JSON templates.

## What it does

- Generates clean print-ready PDFs from JSON templates and JSON record data.
- Supports text, Code 128, EAN-13, QR, and images.
- Paginates automatically for batch runs.
- Uses reusable JSON templates for flexible layouts.
- Imports PDF-based label templates, including Avery-style layout PDFs, into reusable JSON template files.
- Includes built-in Avery templates by product code for common sheet layouts.
- Accepts geometric values in either millimeters or inches.

## Why PDF import only

`.doc`, `.psd`, and `.ai` are not reliable inputs for a Python-only CLI without depending on proprietary applications or lossy reverse engineering. PDF is the practical import format here because page geometry and vector label outlines are accessible directly in Python.

The importer detects both vector label rectangles and line-only guides. If a source PDF still cannot be auto-detected, you can provide `--rows`, `--cols`, `--label-width-mm`, and `--label-height-mm`, plus any margin or gap overrides you need.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development:

```bash
python -m pip install -e '.[dev]'
```

For the interactive Streamlit web interface:

```bash
python -m pip install -e '.[ui]'
```

### Launching the Web Interface

Launch the Streamlit app:

```bash
label-sheet-ui
```

or equivalently:

```bash
label-sheet ui
```

The UI opens at `http://127.0.0.1:8501` with template pickers, page settings, a JSON data editor with CSV/JSON import, a live rendered preview, and a PDF download button. It uses the same Python rendering pipeline as the CLI, so template selection and output stay consistent.

Templates now support two local categories under `templates/`:

- `templates/labels/` for label sheet geometry templates
- `templates/layouts/` for text layout templates that contain only `elements`

Bare template names are resolved from `templates/`, `templates/labels/`, and `templates/layouts/`, so older top-level templates continue to work too.

For template geometry, use either `_mm` or `_in` keys such as `width_mm` or `width_in`. The same applies to grid and element positions and sizes. Internally the renderer normalizes everything to millimeters.

## Generate labels

Render a full template and a record set into a PDF:

```bash
label-sheet generate basic_template out.pdf --records examples/basic_records.json
```

An inch-based example template is also included:

```bash
label-sheet generate basic_template_inch out.pdf --records examples/basic_records.json
```

Render a label template with a separate text layout template:

```bash
label-sheet generate imported_template2 out.pdf --layout-template basic_address_layout --records examples/basic_records.json
```

Launch the web UI:

```bash
label-sheet-ui
```

Render a single page using static values embedded in the template:

```bash
label-sheet generate basic_template out.pdf
```

Rotate the output PDF for landscape printing:

```bash
label-sheet generate basic_template out.pdf --orientation landscape
```

If the rendered page is upside down, rotate the final PDF page by 180 degrees:

```bash
label-sheet generate basic_template out.pdf --orientation landscape --page-rotation 180
```

Rotate all text elements by 90 degrees while rendering:

```bash
label-sheet generate basic_template out.pdf --text-rotation 90
```

Show label boundaries while tuning layout:

```bash
label-sheet generate basic_template out.pdf --records examples/basic_records.json --draw-border
```

`--outline-slots` remains available as a compatibility alias.

## Avery Templates

List the built-in Avery templates plus local label and text layout templates:

```bash
label-sheet list-templates
```

Create a reusable template skeleton directly from a product code:

```bash
label-sheet avery-template 5160 avery-5160.json
```

With the default filename-only form above, the template is written to `templates/labels/avery-5160.json`.

Supported built-in Avery template groups currently include these common layouts:

- `5160`, `5260`, `5960`, `8160`
- `5161`, `5261`, `8161`
- `5163`, `5263`, `5963`, `8163`
- `5164`, `5264`, `8164`

## Import an Avery-style PDF template

If the PDF contains detectable label rectangles or line guides, import is automatic:

```bash
label-sheet import-template avery-template.pdf imported_template.json
```

If auto-detection is incomplete, provide overrides:

```bash
label-sheet import-template avery-template.pdf imported_template.json --rows 10 --cols 3 --label-width-mm 66.675 --label-height-mm 25.4 --margin-left-mm 4.7625 --margin-top-mm 12.7 --gap-x-mm 3.175
```

You can provide the same geometry in inches instead:

```bash
label-sheet import-template avery-template.pdf imported_template.json --rows 10 --cols 3 --label-width-in 2.625 --label-height-in 1 --margin-left-in 0.1875 --margin-top-in 0.5 --gap-x-in 0.125
```

If you already know the product code, you can use it as a fallback during import:

```bash
label-sheet import-template avery-template.pdf imported_template.json --template-code 5160
```

Filename-only imports are written to `templates/labels/`, added to `list-templates`, and can be used directly with `generate`, for example `label-sheet generate imported_template out.pdf`.

The generated label template contains the page size and normalized grid geometry. You can then add `elements` directly, or pair it with a text layout template via `--layout-template`.

## Text Layout Templates

Text layout templates live in `templates/layouts/` and contain only the `elements` that should be rendered inside each label slot.

For `text` elements, layout templates support `align` values of `left`, `center`, `right`, and `justify`, plus an optional `rotation_deg` value for rotated text. If omitted, alignment defaults to `left` and rotation defaults to `0`.

```json
{
  "template_type": "text-layout",
  "name": "basic-address-layout",
  "elements": [
    {
      "type": "text",
      "field": "name",
      "x_mm": 4,
      "y_mm": 3,
      "width_mm": 58,
      "height_mm": 8,
      "font_name": "Helvetica-Bold",
      "font_size_pt": 11,
      "align": "center",
      "rotation_deg": 90
    }
  ]
}
```

## Web Frontend

The Streamlit UI lets you:

- select a label template and optionally layer on a text layout template
- override page margins, output orientation, page rotation, and text rotation before exporting
- start the data editor with a JSON document seeded from the detected template schema
- import label data from JSON or CSV, with CSV converted into the JSON document format used by the UI
- edit structured label data in a plain-text JSON editor in the browser
- preview the first page from the same Python rendering pipeline used by the CLI
- download the current workspace as a print-ready PDF

The web UI uses the same rendering code as the CLI, so template selection and output stay consistent. A minimal FastAPI JSON API (`label_sheet_generator.web_api`) is also available for automation and tests, independent of the Streamlit UI.

## Template format

Templates may use `_mm` or `_in` for any geometric field. The example below uses millimeters.

```json
{
  "name": "shipping-labels",
  "page": {
    "width_mm": 215.9,
    "height_mm": 279.4
  },
  "grid": {
    "rows": 10,
    "cols": 3,
    "margin_left_mm": 4.7625,
    "margin_top_mm": 12.7,
    "margin_right_mm": 4.7625,
    "margin_bottom_mm": 12.7,
    "gap_x_mm": 3.175,
    "gap_y_mm": 0,
    "label_width_mm": 66.675,
    "label_height_mm": 25.4
  },
  "elements": [
    {
      "type": "text",
      "field": "name",
      "x_mm": 4,
      "y_mm": 3,
      "width_mm": 58,
      "height_mm": 8,
      "font_name": "Helvetica-Bold",
      "font_size_pt": 11
    },
    {
      "type": "text",
      "template": "{address_1}\\n{address_2}",
      "x_mm": 4,
      "y_mm": 12,
      "width_mm": 58,
      "height_mm": 8,
      "font_size_pt": 8
    },
    {
      "type": "barcode",
      "field": "sku",
      "barcode_type": "code128",
      "x_mm": 4,
      "y_mm": 20,
      "width_mm": 50,
      "height_mm": 10
    },
    {
      "type": "image",
      "field": "logo_path",
      "x_mm": 54,
      "y_mm": 2,
      "width_mm": 10,
      "height_mm": 10,
      "preserve_aspect": true
    }
  ]
}
```

## Record format

Use a top-level array:

```json
[
  {
    "name": "Ada Lovelace",
    "address_1": "12 Analytical Engine Way",
    "address_2": "London",
    "sku": "AL-1001",
    "logo_path": "assets/logo.png"
  }
]
```

Or wrap it in a `records` object if you prefer.

## Development notes

- Coordinates inside each label are measured from the top-left corner and can be expressed with `_mm` or `_in` keys.
- Page and grid geometry can also be expressed with `_mm` or `_in` keys.
- Image paths can be relative to either the template file or the records file.
- Empty or missing fields render as blank content.

## Run tests

```bash
pytest
```
