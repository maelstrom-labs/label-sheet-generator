# Label Sheet Generator - Design & Architecture

This document outlines the design system, user interface architecture, and styling techniques used in the Label Sheet Generator's web application.

## 1. Overview
The application uses **Streamlit** as its web framework. Streamlit renders the entire workspace from a single Python script (`streamlit_app.py`) that calls directly into the shared `label_sheet_generator.workspace` rendering pipeline, keeping the UI streamlined, modern, and simple to maintain. A minimal FastAPI JSON API (`web_api.py`) remains available separately for automation and tests, but it is not the interactive UI.

## 2. Design System & Theming

The application uses Streamlit's native theming system (`.streamlit/config.toml`) rather than custom CSS overrides:
*   **Accent color**: A clean blue (`#2563eb`) primary color used for buttons, links, and field-name pills.
*   **Light, high-contrast base**: White background with a light slate secondary background for sidebar and containers.
*   **Minimal custom CSS**: A small stylesheet injected via `st.markdown` hides Streamlit's default menu/footer chrome, tightens page padding, and styles field-name "pills" and the empty-preview placeholder. Everything else uses idiomatic Streamlit widgets and layout primitives (no framework-specific component libraries).

## 3. Workspace Layout

The application is structured into three primary zones:

### Sidebar (Controls & Export)
*   **Brand header**: Title and short caption.
*   **Templates**: Selectboxes for the Label Template and Text Layout.
*   **Page settings**: Orientation radio (Portrait/Landscape), Page Rotation selectbox, border toggle, and an optional text-rotation override.
*   **Page margins**: Collapsible expander with Top/Right/Bottom/Left number inputs (mm).
*   **Filename**: Text input controlling the downloaded PDF's file name.

### Left Column: Data
*   **Uploader**: `st.file_uploader` accepting CSV or JSON, converted to the shared JSON record document format.
*   **Field pills**: Small badges listing the fields detected in the active template.
*   **JSON editor**: A plain `st.text_area` bound to session state, with "Format JSON" and "Reset to sample" actions.
*   **Validation feedback**: Inline `st.error`/`st.warning` messages for JSON parse errors and missing/extra fields.

### Right Column: Live Preview
*   **Preview image**: The first rendered page (PNG) shown via `st.image`, regenerated whenever inputs change.
*   **Metrics**: `st.metric` cards for label count and page count.
*   **Technical info**: A bordered container summarizing orientation, rotation, template, and layout.
*   **Download**: `st.download_button` exporting the full print-ready PDF.

## 4. State Management

Streamlit reruns the script top-to-bottom on every interaction. The app keeps a `_template_signature` tuple (label key, layout key) in `st.session_state`; when it changes, the data document and margins are reset to the newly selected template's defaults. All other widgets (page settings, JSON editor, margins) are bound directly to `st.session_state` via their `key=` argument, so edits persist naturally across reruns without extra plumbing.
