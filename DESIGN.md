# Label Sheet Generator - Design & Architecture

This document outlines the design system, user interface architecture, and styling techniques used in the Label Sheet Generator's web application. 

## 1. Overview
The application uses **Streamlit** as its core web framework. However, to break away from the default Streamlit appearance and achieve a highly polished, professional "Stitch-inspired" interface, the app employs extensive custom CSS injection. The result is a modern, responsive, two-pane workspace with a persistent sidebar, native-feeling panels, and dynamic theming.

## 2. Design System & Theming

The application implements a custom token-based design system featuring two primary modes:
*   **Deep-Ocean (Dark Mode)**: A sleek, low-contrast dark theme using deep blues (`#0c1626`), cyan accents (`#19b7d9`), and subtle radial gradients to provide depth.
*   **Mist (Light Mode)**: A clean, high-contrast light theme using crisp whites, light slate grays, and bold blue-cyan accents.

### CSS Injection Strategy
Themes are applied by injecting dynamic CSS directly into the Streamlit DOM via `st.markdown(..., unsafe_allow_html=True)`.

The `THEME_PALETTES` dictionary in `streamlit_app.py` defines the color tokens for both modes. These tokens are mapped to CSS variables (e.g., `--app-bg`, `--panel-bg`, `--accent-primary`) globally within the `:root` scope.

### Overriding Streamlit Defaults
To seamlessly integrate the design system, specific Streamlit `[data-testid="..."]` DOM elements are targeted and overridden:
*   **Backgrounds**: `[data-testid="stAppViewContainer"]` receives custom linear and radial gradients.
*   **Sidebar**: `[data-testid="stSidebar"]` is restyled with a custom width, borders, and gradient backgrounds.
*   **Inputs & Buttons**: `stFileUploader`, `stTextArea`, `stSelectbox`, and `.stButton` instances are stripped of their default shadows and borders, receiving rounded corners (`14px` - `20px`), custom borders, and matching hover states that align with modern UI libraries.

## 3. Workspace Layout

The application is structured into three primary structural zones:

### Sidebar (Controls & Export)
*   **Brand Header**: A custom HTML block showing the app logo and title.
*   **Configuration**: Selectors for Label Templates and Text Layouts.
*   **Page Settings**: Orientation (Portrait/Landscape), Page Rotation, and rendering toggles.
*   **Export**: The PDF download button sits at the bottom of the sidebar.

### Left Pane: Data Input
*   **Uploader**: A custom-styled dropzone for CSV and JSON files.
*   **JSON Editor**: Powered by `streamlit_ace`, this provides a syntax-highlighted code editor for the schema and records. It includes a custom "macOS-style" traffic light header to simulate an IDE window.
*   **Form Actions**: Controls to format JSON, reset the workspace, or manually trigger a render.

### Right Pane: Live Preview
*   **Preview Canvas**: Uses a custom CSS checkerboard background to emulate a design tool's artboard. The generated PDF is converted to a PNG using `pypdfium2` and displayed here.
*   **Zoom Controls**: Allows visual scaling of the preview image without affecting the actual PDF output.
*   **Stats Grid**: A dashboard-style metric row displaying the total number of labels, pages, and active technical configurations (e.g., orientation, template name).

## 4. Technical Workarounds & Known Constraints

*   **Stacking Context**: Streamlit's default stacking context can conflict with full-page background overlays. To achieve the deep-ocean background glows without masking the UI (which initially caused a "black screen" bug), the app applies `isolation: isolate` to the `.stApp` container and pushes the decorative `::before` pseudo-elements back using `z-index: -1`.
*   **Streamlit Limitations**: 
    *   Streamlit's internal components (like drop-downs and file uploaders) cannot have their internal HTML structures deeply customized. We rely on CSS attribute targeting and pseudo-classes to style them as closely to the design mockups as possible.
    *   The download button moves with the sidebar scroll rather than remaining sticky at the absolute bottom viewport edge due to Streamlit's flex layout configurations.