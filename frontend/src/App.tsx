import { startTransition, useDeferredValue, useEffect, useState } from "react";


type TemplateOption = {
  key: string;
  template_name: string | null;
  template_type: "label" | "text-layout";
  display_name: string;
};

type BootstrapResponse = {
  label_templates: TemplateOption[];
  layout_templates: TemplateOption[];
};

type WorkspaceConfigResponse = {
  label_template_name: string;
  layout_name: string;
  fields: string[];
  labels_per_page: number;
  preview_settings_error: string | null;
  document: string;
  margin_top_mm: number;
  margin_right_mm: number;
  margin_bottom_mm: number;
  margin_left_mm: number;
};

type PreviewResponse = {
  label_template_name: string;
  layout_name: string;
  labels: number;
  pages: number;
  fields: string[];
  missing_fields: string[];
  extra_fields: string[];
  preview_settings_error: string | null;
  record_document_error: string | null;
  render_runtime_error: string | null;
  preview_image_base64: string | null;
};

type RenderRequest = {
  label_template_key: string;
  layout_template_key: string | null;
  data_document: string;
  margin_top_mm: number;
  margin_right_mm: number;
  margin_bottom_mm: number;
  margin_left_mm: number;
  page_orientation: "portrait" | "landscape";
  page_rotation_deg: 0 | 90 | 180 | 270;
  text_rotation_deg: number | null;
  outline_slots: boolean;
  download_filename: string;
};

type Margins = {
  top: number;
  right: number;
  bottom: number;
  left: number;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "";


async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail ?? response.statusText);
  }

  return response.json() as Promise<T>;
}


function buildRenderRequest(state: {
  labelTemplateKey: string;
  layoutTemplateKey: string | null;
  dataDocument: string;
  margins: Margins;
  pageOrientation: "portrait" | "landscape";
  pageRotationDeg: 0 | 90 | 180 | 270;
  textRotationEnabled: boolean;
  textRotationDeg: number;
  outlineSlots: boolean;
  downloadFilename: string;
}): RenderRequest {
  return {
    label_template_key: state.labelTemplateKey,
    layout_template_key: state.layoutTemplateKey,
    data_document: state.dataDocument,
    margin_top_mm: state.margins.top,
    margin_right_mm: state.margins.right,
    margin_bottom_mm: state.margins.bottom,
    margin_left_mm: state.margins.left,
    page_orientation: state.pageOrientation,
    page_rotation_deg: state.pageRotationDeg,
    text_rotation_deg: state.textRotationEnabled ? state.textRotationDeg : null,
    outline_slots: state.outlineSlots,
    download_filename: state.downloadFilename,
  };
}


export default function App() {
  const [labelTemplates, setLabelTemplates] = useState<TemplateOption[]>([]);
  const [layoutTemplates, setLayoutTemplates] = useState<TemplateOption[]>([]);
  const [labelTemplateKey, setLabelTemplateKey] = useState("");
  const [layoutTemplateKey, setLayoutTemplateKey] = useState<string | null>(null);
  const [config, setConfig] = useState<WorkspaceConfigResponse | null>(null);
  const [dataDocument, setDataDocument] = useState("");
  const [margins, setMargins] = useState<Margins>({ top: 0, right: 0, bottom: 0, left: 0 });
  const [pageOrientation, setPageOrientation] = useState<"portrait" | "landscape">("portrait");
  const [pageRotationDeg, setPageRotationDeg] = useState<0 | 90 | 180 | 270>(0);
  const [textRotationEnabled, setTextRotationEnabled] = useState(false);
  const [textRotationDeg, setTextRotationDeg] = useState(0);
  const [outlineSlots, setOutlineSlots] = useState(true);
  const [downloadFilename, setDownloadFilename] = useState("labels.pdf");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingConfig, setLoadingConfig] = useState(false);
  const [renderingPreview, setRenderingPreview] = useState(false);
  const [downloadingPdf, setDownloadingPdf] = useState(false);
  const [uploadMessage, setUploadMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const deferredDocument = useDeferredValue(dataDocument);

  useEffect(() => {
    let ignore = false;

    async function loadBootstrap() {
      try {
        const payload = await fetchJson<BootstrapResponse>("/api/bootstrap", { method: "GET" });
        if (ignore) {
          return;
        }
        setLabelTemplates(payload.label_templates);
        setLayoutTemplates(payload.layout_templates);
        if (payload.label_templates.length > 0) {
          setLabelTemplateKey(payload.label_templates[0].key);
        }
      } catch (error) {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : "Unable to load templates.");
        }
      } finally {
        if (!ignore) {
          setLoading(false);
        }
      }
    }

    void loadBootstrap();
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (!labelTemplateKey) {
      return;
    }

    let ignore = false;

    async function loadConfig() {
      setLoadingConfig(true);
      setUploadMessage(null);
      setErrorMessage(null);
      try {
        const payload = await fetchJson<WorkspaceConfigResponse>("/api/workspace/config", {
          method: "POST",
          body: JSON.stringify({
            label_template_key: labelTemplateKey,
            layout_template_key: layoutTemplateKey,
          }),
        });
        if (ignore) {
          return;
        }
        startTransition(() => {
          setConfig(payload);
          setDataDocument(payload.document);
          setMargins({
            top: payload.margin_top_mm,
            right: payload.margin_right_mm,
            bottom: payload.margin_bottom_mm,
            left: payload.margin_left_mm,
          });
          setPreview(null);
        });
      } catch (error) {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : "Unable to load the workspace.");
        }
      } finally {
        if (!ignore) {
          setLoadingConfig(false);
        }
      }
    }

    void loadConfig();
    return () => {
      ignore = true;
    };
  }, [labelTemplateKey, layoutTemplateKey]);

  async function requestPreview(documentText = deferredDocument) {
    if (!labelTemplateKey) {
      return;
    }

    setRenderingPreview(true);
    setErrorMessage(null);
    try {
      const payload = await fetchJson<PreviewResponse>("/api/render/preview", {
        method: "POST",
        body: JSON.stringify(
          buildRenderRequest({
            labelTemplateKey,
            layoutTemplateKey,
            dataDocument: documentText,
            margins,
            pageOrientation,
            pageRotationDeg,
            textRotationEnabled,
            textRotationDeg,
            outlineSlots,
            downloadFilename,
          }),
        ),
      });
      startTransition(() => {
        setPreview(payload);
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to render preview.");
    } finally {
      setRenderingPreview(false);
    }
  }

  useEffect(() => {
    if (!labelTemplateKey || !config) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      void requestPreview(deferredDocument);
    }, 300);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [
    config,
    deferredDocument,
    labelTemplateKey,
    layoutTemplateKey,
    margins,
    pageOrientation,
    pageRotationDeg,
    textRotationEnabled,
    textRotationDeg,
    outlineSlots,
  ]);

  async function handleImport(file: File | null) {
    if (!file || !config) {
      return;
    }

    const formData = new FormData();
    formData.append("file", file);
    formData.append("fields", JSON.stringify(config.fields));

    setErrorMessage(null);
    setUploadMessage(null);

    try {
      const response = await fetch(`${API_BASE}/api/workspace/import`, {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(payload.detail ?? response.statusText);
      }
      const payload = (await response.json()) as { document: string };
      setDataDocument(payload.document);
      setUploadMessage(file.name.endsWith(".csv") ? "Imported CSV into the editor." : "Imported JSON into the editor.");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to import file.");
    }
  }

  async function handleDownloadPdf() {
    if (!labelTemplateKey) {
      return;
    }

    setDownloadingPdf(true);
    setErrorMessage(null);

    try {
      const response = await fetch(`${API_BASE}/api/render/pdf`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(
          buildRenderRequest({
            labelTemplateKey,
            layoutTemplateKey,
            dataDocument,
            margins,
            pageOrientation,
            pageRotationDeg,
            textRotationEnabled,
            textRotationDeg,
            outlineSlots,
            downloadFilename,
          }),
        ),
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(payload.detail ?? response.statusText);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = downloadFilename.endsWith(".pdf") ? downloadFilename : `${downloadFilename}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to generate PDF.");
    } finally {
      setDownloadingPdf(false);
    }
  }

  function formatJsonDocument() {
    try {
      const formatted = JSON.stringify(JSON.parse(dataDocument), null, 2);
      setDataDocument(formatted);
    } catch {
      setErrorMessage("The JSON editor contains invalid JSON.");
    }
  }

  function resetToSchema() {
    if (!config) {
      return;
    }
    setDataDocument(config.document);
    setUploadMessage(null);
  }

  if (loading) {
    return <div className="app-shell"><div className="loading-state">Loading workspace…</div></div>;
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">
          <div className="brand-icon">▢</div>
          <div>
            <h1>Label Sheet Generator</h1>
          </div>
        </div>
        <div className="profile-chip">
          <div className="profile-avatar">ML</div>
          <div className="profile-caret">▾</div>
        </div>
      </header>

      <main className="workspace-grid">
        <section className="panel panel-sidebar">
          <div className="panel-header">
            <h2>Templates</h2>
          </div>

          <label className="control-group">
            <span>Label template</span>
            <select value={labelTemplateKey} onChange={(event) => setLabelTemplateKey(event.target.value)}>
              {labelTemplates.map((template) => (
                <option key={template.key} value={template.key}>
                  {template.template_name ?? template.key}
                </option>
              ))}
            </select>
          </label>

          <label className="control-group">
            <span>Text layout template</span>
            <select
              value={layoutTemplateKey ?? ""}
              onChange={(event) => setLayoutTemplateKey(event.target.value === "" ? null : event.target.value)}
            >
              <option value="">Default layout</option>
              {layoutTemplates.map((template) => (
                <option key={template.key} value={template.key}>
                  {template.template_name ?? template.key}
                </option>
              ))}
            </select>
          </label>

          <div className="section-separator" />

          <div className="panel-header compact">
            <h2>Page Settings</h2>
          </div>

          <div className="control-group">
            <span>Page orientation</span>
            <div className="segmented-grid two-up">
              <button
                className={pageOrientation === "portrait" ? "segment active" : "segment"}
                onClick={() => setPageOrientation("portrait")}
                type="button"
              >
                Portrait
              </button>
              <button
                className={pageOrientation === "landscape" ? "segment active" : "segment"}
                onClick={() => setPageOrientation("landscape")}
                type="button"
              >
                Landscape
              </button>
            </div>
          </div>

          <div className="control-group">
            <span>Page rotation</span>
            <div className="segmented-grid four-up">
              {[0, 90, 180, 270].map((rotation) => (
                <button
                  key={rotation}
                  className={pageRotationDeg === rotation ? "segment active" : "segment"}
                  onClick={() => setPageRotationDeg(rotation as 0 | 90 | 180 | 270)}
                  type="button"
                >
                  {rotation}°
                </button>
              ))}
            </div>
          </div>

          <div className="control-group">
            <span>Page margins</span>
            <div className="margin-grid">
              {([
                ["Top", "top"],
                ["Right", "right"],
                ["Bottom", "bottom"],
                ["Left", "left"],
              ] as const).map(([label, key]) => (
                <label key={key} className="compact-input">
                  <span>{label}</span>
                  <input
                    type="number"
                    step="0.5"
                    value={margins[key]}
                    onChange={(event) => setMargins((current) => ({ ...current, [key]: Number(event.target.value) }))}
                  />
                </label>
              ))}
            </div>
          </div>

          <label className="toggle-row">
            <span>Override text rotation</span>
            <input
              checked={textRotationEnabled}
              onChange={(event) => setTextRotationEnabled(event.target.checked)}
              type="checkbox"
            />
          </label>
          {textRotationEnabled ? (
            <label className="control-group compact-gap">
              <span>Text rotation</span>
              <input
                type="number"
                step="90"
                value={textRotationDeg}
                onChange={(event) => setTextRotationDeg(Number(event.target.value))}
              />
            </label>
          ) : null}

          <label className="toggle-row">
            <span>Draw label borders</span>
            <input checked={outlineSlots} onChange={(event) => setOutlineSlots(event.target.checked)} type="checkbox" />
          </label>

          <label className="control-group compact-gap">
            <span>Download filename</span>
            <input value={downloadFilename} onChange={(event) => setDownloadFilename(event.target.value)} type="text" />
          </label>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>Data Input</h2>
          </div>

          <label className="upload-surface">
            <input
              accept=".csv,.json"
              className="hidden-input"
              onChange={(event) => void handleImport(event.target.files?.[0] ?? null)}
              type="file"
            />
            <div className="upload-icon">⇪</div>
            <strong>Upload CSV/JSON</strong>
            <span>Choose a file to seed the editor with records.</span>
          </label>

          <div className="editor-shell">
            <div className="editor-toolbar">
              <strong>Code editor</strong>
              <div className="toolbar-actions">
                <button onClick={formatJsonDocument} type="button">Format JSON</button>
                <button onClick={resetToSchema} type="button">Reset to Schema</button>
              </div>
            </div>
            <textarea
              className="code-editor"
              onChange={(event) => setDataDocument(event.target.value)}
              spellCheck={false}
              value={dataDocument}
            />
          </div>

          <div className="status-stack">
            {loadingConfig ? <div className="notice">Refreshing template configuration…</div> : null}
            {uploadMessage ? <div className="notice success">{uploadMessage}</div> : null}
            {errorMessage ? <div className="notice error">{errorMessage}</div> : null}
            {preview?.missing_fields.length ? (
              <div className="notice warning">Missing fields: {preview.missing_fields.join(", ")}</div>
            ) : null}
            {preview?.extra_fields.length ? (
              <div className="notice info">Unused fields: {preview.extra_fields.join(", ")}</div>
            ) : null}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header with-meta">
            <h2>Live Preview</h2>
            <div className="preview-meta">
              {preview ? <span>{preview.labels} labels</span> : null}
              {preview ? <span>{preview.pages} pages</span> : null}
            </div>
          </div>

          <div className="preview-stage">
            {preview?.preview_image_base64 ? (
              <img
                alt="Rendered label preview"
                className="preview-image"
                src={`data:image/png;base64,${preview.preview_image_base64}`}
              />
            ) : (
              <div className="preview-placeholder">
                <strong>Preview not available</strong>
                <span>
                  {preview?.preview_settings_error ?? preview?.record_document_error ?? preview?.render_runtime_error ?? "The preview will populate once the current configuration can render."}
                </span>
              </div>
            )}
          </div>

          <div className="preview-summary">
            <div>
              <span className="summary-label">Label Template</span>
              <strong>{preview?.label_template_name ?? config?.label_template_name ?? "—"}</strong>
            </div>
            <div>
              <span className="summary-label">Layout</span>
              <strong>{preview?.layout_name ?? config?.layout_name ?? "—"}</strong>
            </div>
          </div>

          <div className="preview-actions">
            <button className="primary-action" disabled={renderingPreview || !labelTemplateKey} onClick={() => void requestPreview(dataDocument)} type="button">
              {renderingPreview ? "Rendering…" : "Render Preview"}
            </button>
            <button className="primary-action secondary" disabled={downloadingPdf || !labelTemplateKey} onClick={() => void handleDownloadPdf()} type="button">
              {downloadingPdf ? "Preparing PDF…" : "Download PDF"}
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}