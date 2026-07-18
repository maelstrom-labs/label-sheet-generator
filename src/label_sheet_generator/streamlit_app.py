"""Streamlit front end for the Label Sheet Generator.

This module renders a single-page Streamlit application that reuses the same
workspace/rendering pipeline as the CLI (see ``label_sheet_generator.workspace``).
Run it with ``label-sheet ui`` or directly via ``streamlit run``.
"""

"""Streamlit front end for the Label Sheet Generator.

This module renders a single-page Streamlit application that reuses the same
workspace/rendering pipeline as the CLI (see ``label_sheet_generator.workspace``).
Run it with ``label-sheet ui`` or directly via ``streamlit run``.
"""

from __future__ import annotations

import json

import streamlit as st

from label_sheet_generator.models import TemplateError
from label_sheet_generator.workspace import (
    convert_import_data_to_json,
    format_template_option,
    get_template_catalog,
    get_workspace_config,
    normalize_download_filename,
    prepare_render_state,
    render_workspace_pdf,
    render_workspace_preview,
)

st.set_page_config(
    page_title="Label Sheet Generator",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
#MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px;}
h1, h2, h3 {letter-spacing: -0.02em;}
.field-pill {
    display: inline-block;
    padding: 0.15rem 0.65rem;
    margin: 0.15rem 0.25rem 0.15rem 0;
    border-radius: 999px;
    background: rgba(37, 99, 235, 0.10);
    color: #2563eb;
    font-size: 0.78rem;
    font-weight: 600;
}
.no-fields {
    color: #94a3b8;
    font-size: 0.85rem;
    font-style: italic;
}
.preview-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 320px;
    border: 1px dashed rgba(100, 116, 139, 0.35);
    border-radius: 0.75rem;
    color: #94a3b8;
    font-size: 0.9rem;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def _load_template_catalog():
    try:
        return get_template_catalog()
    except TemplateError as exc:
        st.error(f"Unable to load templates: {exc}")
        st.stop()


def _reset_workspace_state(config) -> None:
    if "data_document" not in st.session_state:
        st.session_state["data_document"] = config.document
    if "margin_top_mm" not in st.session_state:
        st.session_state["margin_top_mm"] = config.margin_top_mm
    if "margin_right_mm" not in st.session_state:
        st.session_state["margin_right_mm"] = config.margin_right_mm
    if "margin_bottom_mm" not in st.session_state:
        st.session_state["margin_bottom_mm"] = config.margin_bottom_mm
    if "margin_left_mm" not in st.session_state:
        st.session_state["margin_left_mm"] = config.margin_left_mm


def _format_json() -> None:
    try:
        parsed = json.loads(st.session_state["data_document"])
        st.session_state["data_document"] = json.dumps(parsed, indent=2)
    except json.JSONDecodeError as exc:
        st.error(f"Invalid JSON: {exc}")


def _handle_upload(config) -> None:
    uploaded_file = st.session_state.get("_uploader")
    if uploaded_file is None:
        return
    upload_signature = (uploaded_file.name, uploaded_file.size)
    if st.session_state.get("_last_upload_signature") == upload_signature:
        return
    st.session_state["_last_upload_signature"] = upload_signature
    try:
        st.session_state["data_document"] = convert_import_data_to_json(
            uploaded_file.name,
            uploaded_file.getvalue(),
            fields=config.fields,
        )
        st.success(f"Loaded records from {uploaded_file.name}")
    except (TemplateError, UnicodeDecodeError) as exc:
        st.error(f"Import failed: {exc}")


def main() -> None:
    label_templates, layout_templates = _load_template_catalog()
    label_options = {
        template.key: format_template_option(template) for template in label_templates
    }
    layout_options: dict[str | None, str] = {None: "Default (use template elements)"}
    layout_options.update(
        {
            template.key: format_template_option(template)
            for template in layout_templates
        }
    )

    with st.sidebar:
        st.markdown("## 🏷️ Label Sheet Generator")
        st.caption("Design, preview, and export print-ready label sheets.")
        st.divider()

        with st.expander("**Templates**", expanded=True):
            st.write("""
                Select the label and text layout templates.
                """)
            label_key = st.selectbox(
                "Label template",
                options=list(label_options),
                format_func=lambda key: label_options[key],
                key="label_template_key",
            )
            layout_key = st.selectbox(
                "Text layout",
                options=list(layout_options),
                format_func=lambda key: layout_options[key],
                key="layout_template_key",
            )

        try:
            config = get_workspace_config(label_key, layout_key)
        except TemplateError as exc:
            st.error(str(exc))
            st.stop()

        template_signature = (label_key, layout_key)
        if st.session_state.get("_template_signature") != template_signature:
            st.session_state["_template_signature"] = template_signature
            _reset_workspace_state(config)

        with st.expander("**Page Settings**", expanded=True):
            st.write("""
                Configure the orientation, rotation, and margins of the label sheet.
                """)
            orientation = st.radio(
                "Orientation",
                options=["portrait", "landscape"],
                format_func=str.title,
                horizontal=True,
                key="page_orientation",
            )
            rotation_deg = st.selectbox(
                "Page rotation",
                options=[0, 90, 180, 270],
                format_func=lambda value: f"{value}°",
                key="page_rotation_deg",
            )
            outline_slots = st.checkbox(
                "Draw label borders", value=True, key="outline_slots"
            )
            override_text_rotation = st.checkbox(
                "Override text rotation", key="override_text_rotation"
            )
            text_rotation_deg = None
            if override_text_rotation:
                text_rotation_deg = st.number_input(
                    "Text rotation (°)", value=0.0, step=90.0, key="text_rotation_deg"
                )

        with st.expander("**Page Margins (mm)**", expanded=True):
            st.write("""
                Adjust the top, right, bottom, and left margins of the label sheet.
                """)
            margin_top_mm = st.number_input(
                "Top", step=0.5, format="%.2f", key="margin_top_mm"
            )
            margin_right_mm = st.number_input(
                "Right", step=0.5, format="%.2f", key="margin_right_mm"
            )
            margin_bottom_mm = st.number_input(
                "Bottom", step=0.5, format="%.2f", key="margin_bottom_mm"
            )
            margin_left_mm = st.number_input(
                "Left", step=0.5, format="%.2f", key="margin_left_mm"
            )

        download_filename = st.text_input(
            "Filename", value="labels", key="download_filename"
        )

    st.title("Design your label sheet")
    st.caption(
        f"Template: {config.label_template_name}  ·  Layout: {config.layout_template_name}"
    )

    data_col, preview_col = st.columns([1, 1], gap="large")

    with data_col:
        st.subheader("Data")
        header_left, header_right = st.columns([3, 1])
        with header_right:
            if st.button("Reset to sample", use_container_width=True):
                _reset_workspace_state(config)
                st.experimental_rerun()

        uploaded_file = st.file_uploader(
            "Import records from CSV or JSON",
            type=["csv", "json"],
            key="_uploader",
            on_change=lambda: _handle_upload(config),
        )

        if config.fields:
            st.markdown(
                "".join(
                    f'<span class="field-pill">{field}</span>'
                    for field in config.fields
                ),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<span class="no-fields">No dynamic fields detected in this template</span>',
                unsafe_allow_html=True,
            )

        st.text_area("Label data (JSON)", key="data_document", height=340)

        format_col, _ = st.columns([1, 3])
        with format_col:
            if st.button("Format JSON", use_container_width=True):
                _format_json()

    if config.preview_settings_error is not None:
        st.error(config.preview_settings_error)
        return

    try:
        render_state = prepare_render_state(
            config,
            data_document=st.session_state["data_document"],
            margin_top_mm=margin_top_mm,
            margin_right_mm=margin_right_mm,
            margin_bottom_mm=margin_bottom_mm,
            margin_left_mm=margin_left_mm,
        )
    except TemplateError as exc:
        with preview_col:
            st.error(str(exc))
        return

    with data_col:
        if render_state.record_document_error is not None:
            st.error(f"JSON error: {render_state.record_document_error}")
        elif render_state.missing_fields:
            st.warning(
                f"Missing fields in data: {', '.join(render_state.missing_fields)}"
            )

    with preview_col:
        st.subheader("Live Preview")

        if not render_state.renderable:
            st.markdown(
                '<div class="preview-placeholder">Add label data to see a preview</div>',
                unsafe_allow_html=True,
            )
            return

        try:
            preview_image_bytes = render_workspace_preview(
                render_state,
                outline_slots=outline_slots,
                page_orientation=orientation,
                page_rotation_deg=rotation_deg,
                text_rotation_deg=text_rotation_deg,
            )
        except (RuntimeError, TemplateError) as exc:
            st.error(f"Rendering error: {exc}")
            return

        st.image(preview_image_bytes, use_container_width=True)

        metric_cols = st.columns(2)
        metric_cols[0].metric("Labels", len(render_state.full_records))
        metric_cols[1].metric("Pages", render_state.page_count)

        with st.container():
            st.caption(
                f"Orientation: {orientation.title()}  ·  Rotation: {rotation_deg}°  ·  "
                f"Template: {config.label_template_name}  ·  Layout: {config.layout_template_name}"
            )

        try:
            pdf_bytes = render_workspace_pdf(
                render_state,
                outline_slots=outline_slots,
                page_orientation=orientation,
                page_rotation_deg=rotation_deg,
                text_rotation_deg=text_rotation_deg,
            )
        except (RuntimeError, TemplateError) as exc:
            st.error(f"PDF export failed: {exc}")
            return

        st.download_button(
            "Download PDF",
            data=pdf_bytes,
            file_name=normalize_download_filename(download_filename),
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )


main()
from __future__ import annotations

import json

import streamlit as st

from label_sheet_generator.models import TemplateError
from label_sheet_generator.workspace import (
    convert_import_data_to_json,
    format_template_option,
    get_template_catalog,
    get_workspace_config,
    normalize_download_filename,
    prepare_render_state,
    render_workspace_pdf,
    render_workspace_preview,
)

st.set_page_config(
    page_title="Label Sheet Generator",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
#MainMenu, footer {visibility: hidden;}
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px;}
h1, h2, h3 {letter-spacing: -0.02em;}
.field-pill {
    display: inline-block;
    padding: 0.15rem 0.65rem;
    margin: 0.15rem 0.25rem 0.15rem 0;
    border-radius: 999px;
    background: rgba(37, 99, 235, 0.10);
    color: #2563eb;
    font-size: 0.78rem;
    font-weight: 600;
}
.no-fields {
    color: #94a3b8;
    font-size: 0.85rem;
    font-style: italic;
}
.preview-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 320px;
    border: 1px dashed rgba(100, 116, 139, 0.35);
    border-radius: 0.75rem;
    color: #94a3b8;
    font-size: 0.9rem;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def _load_template_catalog():
    try:
        return get_template_catalog()
    except TemplateError as exc:
        st.error(f"Unable to load templates: {exc}")
        st.stop()


def _reset_workspace_state(config) -> None:
    if "data_document" not in st.session_state:
        st.session_state["data_document"] = config.document
    if "margin_top_mm" not in st.session_state:
        st.session_state["margin_top_mm"] = config.margin_top_mm
    if "margin_right_mm" not in st.session_state:
        st.session_state["margin_right_mm"] = config.margin_right_mm
    if "margin_bottom_mm" not in st.session_state:
        st.session_state["margin_bottom_mm"] = config.margin_bottom_mm
    if "margin_left_mm" not in st.session_state:
        st.session_state["margin_left_mm"] = config.margin_left_mm


def _format_json() -> None:
    try:
        parsed = json.loads(st.session_state["data_document"])
        st.session_state["data_document"] = json.dumps(parsed, indent=2)
    except json.JSONDecodeError as exc:
        st.error(f"Invalid JSON: {exc}")


def _handle_upload(config) -> None:
    uploaded_file = st.session_state.get("_uploader")
    if uploaded_file is None:
        return
    upload_signature = (uploaded_file.name, uploaded_file.size)
    if st.session_state.get("_last_upload_signature") == upload_signature:
        return
    st.session_state["_last_upload_signature"] = upload_signature
    try:
        st.session_state["data_document"] = convert_import_data_to_json(
            uploaded_file.name,
            uploaded_file.getvalue(),
            fields=config.fields,
        )
        st.success(f"Loaded records from {uploaded_file.name}")
    except (TemplateError, UnicodeDecodeError) as exc:
        st.error(f"Import failed: {exc}")


def main() -> None:
    label_templates, layout_templates = _load_template_catalog()
    label_options = {
        template.key: format_template_option(template) for template in label_templates
    }
    layout_options: dict[str | None, str] = {None: "Default (use template elements)"}
    layout_options.update(
        {
            template.key: format_template_option(template)
            for template in layout_templates
        }
    )

    with st.sidebar:
        st.markdown("## 🏷️ Label Sheet Generator")
        st.caption("Design, preview, and export print-ready label sheets.")
        st.divider()

        with st.expander("**Templates**", expanded=True):
            st.write("""
                Select the label and text layout templates.
                """)
            label_key = st.selectbox(
                "Label template",
                options=list(label_options),
                format_func=lambda key: label_options[key],
                key="label_template_key",
            )
            layout_key = st.selectbox(
                "Text layout",
                options=list(layout_options),
                format_func=lambda key: layout_options[key],
                key="layout_template_key",
            )

        try:
            config = get_workspace_config(label_key, layout_key)
        except TemplateError as exc:
            st.error(str(exc))
            st.stop()

        template_signature = (label_key, layout_key)
        if st.session_state.get("_template_signature") != template_signature:
            st.session_state["_template_signature"] = template_signature
            _reset_workspace_state(config)

        with st.expander("**Page Settings**", expanded=True):
            st.write("""
                Configure the orientation, rotation, and margins of the label sheet.
                """)
            orientation = st.radio(
                "Orientation",
                options=["portrait", "landscape"],
                format_func=str.title,
                horizontal=True,
                key="page_orientation",
            )
            rotation_deg = st.selectbox(
                "Page rotation",
                options=[0, 90, 180, 270],
                format_func=lambda value: f"{value}°",
                key="page_rotation_deg",
            )
            outline_slots = st.checkbox(
                "Draw label borders", value=True, key="outline_slots"
            )
            override_text_rotation = st.checkbox(
                "Override text rotation", key="override_text_rotation"
            )
            text_rotation_deg = None
            if override_text_rotation:
                text_rotation_deg = st.number_input(
                    "Text rotation (°)", value=0.0, step=90.0, key="text_rotation_deg"
                )

        with st.expander("**Page Margins (mm)**", expanded=True):
            st.write("""
                Adjust the top, right, bottom, and left margins of the label sheet.
                """)
            margin_top_mm = st.number_input(
                "Top", step=0.5, format="%.2f", key="margin_top_mm"
            )
            margin_right_mm = st.number_input(
                "Right", step=0.5, format="%.2f", key="margin_right_mm"
            )
            margin_bottom_mm = st.number_input(
                "Bottom", step=0.5, format="%.2f", key="margin_bottom_mm"
            )
            margin_left_mm = st.number_input(
                "Left", step=0.5, format="%.2f", key="margin_left_mm"
            )

        download_filename = st.text_input(
            "Filename", value="labels", key="download_filename"
        )

    st.title("Design your label sheet")
    st.caption(
        f"Template: {config.label_template_name}  ·  Layout: {config.layout_template_name}"
    )

    data_col, preview_col = st.columns([1, 1], gap="large")

    with data_col:
        st.subheader("Data")
        header_left, header_right = st.columns([3, 1])
        with header_right:
            if st.button("Reset to sample", use_container_width=True):
                _reset_workspace_state(config)
                st.experimental_rerun()

        uploaded_file = st.file_uploader(
            "Import records from CSV or JSON",
            type=["csv", "json"],
            key="_uploader",
            on_change=lambda: _handle_upload(config),
        )

        if config.fields:
            st.markdown(
                "".join(
                    f'<span class="field-pill">{field}</span>'
                    for field in config.fields
                ),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<span class="no-fields">No dynamic fields detected in this template</span>',
                unsafe_allow_html=True,
            )

        st.text_area("Label data (JSON)", key="data_document", height=340)

        format_col, _ = st.columns([1, 3])
        with format_col:
            if st.button("Format JSON", use_container_width=True):
                _format_json()

    if config.preview_settings_error is not None:
        st.error(config.preview_settings_error)
        return

    try:
        render_state = prepare_render_state(
            config,
            data_document=st.session_state["data_document"],
            margin_top_mm=margin_top_mm,
            margin_right_mm=margin_right_mm,
            margin_bottom_mm=margin_bottom_mm,
            margin_left_mm=margin_left_mm,
        )
    except TemplateError as exc:
        with preview_col:
            st.error(str(exc))
        return

    with data_col:
        if render_state.record_document_error is not None:
            st.error(f"JSON error: {render_state.record_document_error}")
        elif render_state.missing_fields:
            st.warning(
                f"Missing fields in data: {', '.join(render_state.missing_fields)}"
            )

    with preview_col:
        st.subheader("Live Preview")

        if not render_state.renderable:
            st.markdown(
                '<div class="preview-placeholder">Add label data to see a preview</div>',
                unsafe_allow_html=True,
            )
            return

        try:
            preview_image_bytes = render_workspace_preview(
                render_state,
                outline_slots=outline_slots,
                page_orientation=orientation,
                page_rotation_deg=rotation_deg,
                text_rotation_deg=text_rotation_deg,
            )
        except (RuntimeError, TemplateError) as exc:
            st.error(f"Rendering error: {exc}")
            return

        st.image(preview_image_bytes, use_container_width=True)

        metric_cols = st.columns(2)
        metric_cols[0].metric("Labels", len(render_state.full_records))
        metric_cols[1].metric("Pages", render_state.page_count)

        with st.container():
            st.caption(
                f"Orientation: {orientation.title()}  ·  Rotation: {rotation_deg}°  ·  "
                f"Template: {config.label_template_name}  ·  Layout: {config.layout_template_name}"
            )

        try:
            pdf_bytes = render_workspace_pdf(
                render_state,
                outline_slots=outline_slots,
                page_orientation=orientation,
                page_rotation_deg=rotation_deg,
                text_rotation_deg=text_rotation_deg,
            )
        except (RuntimeError, TemplateError) as exc:
            st.error(f"PDF export failed: {exc}")
            return

        st.download_button(
            "Download PDF",
            data=pdf_bytes,
            file_name=normalize_download_filename(download_filename),
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )


main()
