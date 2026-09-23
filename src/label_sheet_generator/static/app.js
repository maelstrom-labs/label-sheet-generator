/**
 * Label Sheet Generator - browser client.
 *
 * One vanilla ES module, no framework, no bundler, no network dependency other
 * than the same-origin JSON/PNG/PDF API. It runs under a strict CSP
 * (default-src 'self'), so there is no inline script, no eval, no new Function
 * and no inline event handler anywhere in this file or in the markup it builds.
 *
 * Trust model: every string that comes back from the API - template names,
 * field names, record values, error messages - is untrusted. The DOM is built
 * exclusively with createElement/createElementNS + textContent. innerHTML and
 * friends are never assigned.
 *
 * Sections, in order:
 *   1. Config and constants      6. Template cards and thumbnails
 *   2. State                     7. Settings rail
 *   3. Utilities                 8. Records editor
 *   4. DOM helpers               9. Preview loop
 *   5. API client               10. Export / 11. Persistence / 12. Boot
 */

// ---------------------------------------------------------------------------
// 1. Config and constants
// ---------------------------------------------------------------------------

/** Same-origin API surface. Kept in one place so a mount prefix is a one-line change. */
const API = Object.freeze({
  bootstrap: '/api/bootstrap',
  template: '/api/templates/',
  parseRecords: '/api/records/parse',
  plan: '/api/render/plan',
  preview: '/api/render/preview',
  pdf: '/api/render/pdf',
});

/** Long enough that a burst of keystrokes is one render, short enough to feel live. */
const PREVIEW_DEBOUNCE_MS = 250;

/** Table inputs commit into the canonical document before the preview debounce runs. */
const TABLE_COMMIT_MS = 150;

/** Writing localStorage on every keystroke is wasteful; the UI state is not precious. */
const PERSIST_DEBOUNCE_MS = 500;

/** A render under this duration would make the progress bar flicker, so we hide it. */
const PROGRESS_DELAY_MS = 400;

/** Honest messaging for cold starts instead of a longer, silent spinner. */
const COLD_START_NOTICE_MS = 3000;

/** Undo windows, in ms, for the two destructive-feeling record operations. */
const UNDO_IMPORT_MS = 10000;
const UNDO_DELETE_MS = 6000;

/** Toasts are transient; long enough to read a sentence, short enough not to nag. */
const TOAST_MS = 8000;

/** Copy-button confirmation flash. */
const COPIED_FLASH_MS = 1600;

/** Object URLs for a downloaded PDF stay alive until the browser has fetched them. */
const OBJECT_URL_TTL_MS = 60000;

/** Long edge of a template thumbnail, in CSS px. Matches --thumb-size in the stylesheet. */
const THUMB_LONG_EDGE_PX = 56;

/** A pathological grid (thousands of tiny cells) would bloat the DOM for no visual gain. */
const THUMB_MAX_CELLS = 600;

/** The search box only earns its space once the list is long enough to need it. */
const SEARCH_VISIBLE_THRESHOLD = 8;

/** Reference sheet width, in CSS px, at which preview scale 1.0 is exactly 1 device px. */
const PREVIEW_SCALE_REFERENCE_PX = 820;

/** Preview scale is quantised so that small resizes do not bust the server-side cache. */
const PREVIEW_SCALE_STEP = 0.5;

/** After this many consecutive transport failures we stop auto-retrying and ask the user. */
const MAX_CONSECUTIVE_FAILURES = 3;

/** Client-side upload guard. The server's real cap arrives in bootstrap.limits. */
const FALLBACK_MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

/** Record documents live in sessionStorage; above this they are simply not persisted. */
const MAX_SESSION_DOCUMENT_BYTES = 256 * 1024;

/** Panel resize bounds and keyboard steps, in CSS px. */
const RECORDS_MIN_H = 160;
const RECORDS_DEFAULT_H = 320;
const RECORDS_STEP_H = 16;
const RECORDS_BIG_STEP_H = 64;

/** Fraction of the viewport the records panel may occupy when dragged to its limit. */
const RECORDS_MAX_VIEWPORT_FRACTION = 0.8;

/** Display-only unit conversion. The wire is always millimetres. */
const MM_PER_INCH = 25.4;

/** Decimal places used when showing lengths, per unit. */
const MM_DECIMALS = 2;
const IN_DECIMALS = 3;

/** Named page sizes, width x height in mm, portrait. Used only for the human-readable label. */
const PAGE_SIZES = Object.freeze([
  { name: 'Letter', w: 215.9, h: 279.4 },
  { name: 'Legal', w: 215.9, h: 355.6 },
  { name: 'Tabloid', w: 279.4, h: 431.8 },
  { name: 'A3', w: 297, h: 420 },
  { name: 'A4', w: 210, h: 297 },
  { name: 'A5', w: 148, h: 210 },
  { name: 'A6', w: 105, h: 148 },
]);

/** Tolerance for matching a page against a named size, in mm. Templates round differently. */
const PAGE_SIZE_TOLERANCE_MM = 0.6;

/** localStorage keys are namespaced so a shared origin cannot collide with us. */
const LS = 'lsg.';

/** Session-scoped record documents: useful across a refresh, surprising across a week. */
const SS_DOCUMENT = 'lsg.document';

const SVG_NS = 'http://www.w3.org/2000/svg';

const THEMES = Object.freeze(['system', 'light', 'dark']);

const ORIENTATIONS = Object.freeze(['portrait', 'landscape']);

const PAGE_ROTATIONS = Object.freeze([0, 90, 180, 270]);

// ---------------------------------------------------------------------------
// 2. State
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} Geometry
 * @property {number} page_width_mm
 * @property {number} page_height_mm
 * @property {number} rows
 * @property {number} cols
 * @property {number} label_width_mm
 * @property {number} label_height_mm
 * @property {number} gap_x_mm
 * @property {number} gap_y_mm
 * @property {number} margin_top_mm
 * @property {number} margin_right_mm
 * @property {number} margin_bottom_mm
 * @property {number} margin_left_mm
 */

/**
 * @typedef {Object} TemplateInfo
 * @property {string} id
 * @property {"label"|"text-layout"} kind
 * @property {string} name
 * @property {"builtin"|"user"|"preset"} source
 * @property {string[]} fields
 * @property {number} labels_per_page
 * @property {"mm"|"in"} units
 * @property {string|null} description
 * @property {string[]} aliases
 * @property {string[]} warnings
 * @property {Geometry|null} geometry
 */

/**
 * @typedef {Object} Plan
 * @property {number} labels
 * @property {number} pages
 * @property {number} labels_per_page
 * @property {string[]} fields
 * @property {string[]} missing_fields
 * @property {string[]} extra_fields
 * @property {{ok:boolean, errors:Array<Object>, warnings:Array<Object>}} geometry
 * @property {Array<{code:string,message:string,row?:number}>} record_warnings
 * @property {string} template_name
 * @property {string} layout_name
 */

/**
 * @typedef {Object} AppState
 * @property {"booting"|"ready"|"failed"} phase
 * @property {ApiError|null} bootError
 * @property {TemplateInfo[]} templates
 * @property {TemplateInfo[]} layouts
 * @property {Array<{id:string,path:string,message:string}>} broken
 * @property {Object} limits
 * @property {string} version
 * @property {Object} features
 * @property {string|null} templateId
 * @property {string|null} layoutId
 * @property {string} document          canonical record JSON, the single source of truth
 * @property {{top:number,right:number,bottom:number,left:number}} margins  always mm
 * @property {{top:number,right:number,bottom:number,left:number}} marginDefaults
 * @property {"portrait"|"landscape"} orientation
 * @property {0|90|180|270} pageRotationDeg
 * @property {number|null} textRotationDeg
 * @property {boolean} outlineSlots
 * @property {string} filename          without the .pdf extension
 * @property {Plan|null} plan
 * @property {Object} ui
 * @property {Object} runtime
 */

/** @type {AppState} */
const state = {
  phase: 'booting',
  bootError: null,
  templates: [],
  layouts: [],
  broken: [],
  limits: {
    max_records: 5000,
    max_pages: 200,
    max_document_bytes: 1024 * 1024,
    max_upload_bytes: FALLBACK_MAX_UPLOAD_BYTES,
    preview_scale_min: 0.5,
    preview_scale_max: 3,
    preview_scale_default: 1.5,
    max_field_value_length: 4000,
  },
  version: '',
  features: {},

  templateId: null,
  layoutId: null,
  document: '',
  margins: { top: 0, right: 0, bottom: 0, left: 0 },
  marginDefaults: { top: 0, right: 0, bottom: 0, left: 0 },
  orientation: 'portrait',
  pageRotationDeg: 0,
  textRotationDeg: null,
  outlineSlots: true,
  filename: 'labels',

  plan: null,

  ui: {
    page: 0,
    unit: 'mm',
    theme: 'system',
    tab: 'table',
    recordsOpen: true,
    recordsHeight: RECORDS_DEFAULT_H,
    search: '',
    showMargins: false,
    dimSheet: false,
  },

  runtime: {
    seq: 0,
    controller: null,
    lastMs: null,
    objectUrl: null,
    previewEtag: null,
    previewKey: null,
    status: 'idle',
    exporting: false,
    failures: 0,
    autoRender: true,
    previewTimer: 0,
    tableTimer: 0,
    persistTimer: 0,
    documentTimer: 0,
    progressTimer: 0,
    coldStartTimer: 0,
    undoTimer: 0,
    escapedTextarea: false,
    messages: [],
  },
};

// ---------------------------------------------------------------------------
// 3. Utilities
// ---------------------------------------------------------------------------

/** @returns {number} value confined to [lo, hi]; NaN collapses to lo. */
function clamp(value, lo, hi) {
  if (!Number.isFinite(value)) return lo;
  return Math.min(hi, Math.max(lo, value));
}

/** Rounds to `places` decimals without exponent surprises for the sizes we handle. */
function round(value, places) {
  const factor = 10 ** places;
  return Math.round((Number(value) + Number.EPSILON) * factor) / factor;
}

/** @returns {string} a length formatted for display in the currently selected unit. */
function formatLength(mm, unit) {
  if (!Number.isFinite(mm)) return '-';
  if (unit === 'in') return round(mm / MM_PER_INCH, IN_DECIMALS).toString();
  return round(mm, MM_DECIMALS).toString();
}

/** Display precision for the dimension readouts, which do not need full precision. */
function formatMm(mm) {
  return Number.isFinite(mm) ? round(mm, 1).toString() : '-';
}

/** @returns {number} a display value converted back to the millimetres the wire expects. */
function toMm(value, unit) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return unit === 'in' ? n * MM_PER_INCH : n;
}

function formatCount(n, singular, plural) {
  const value = Number.isFinite(n) ? n : 0;
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

/** Trailing-edge debounce that exposes its timer id so callers can cancel. */
function debounce(fn, ms, slot) {
  return (...args) => {
    window.clearTimeout(state.runtime[slot]);
    state.runtime[slot] = window.setTimeout(() => fn(...args), ms);
  };
}

/** @returns {string} the human name of a page size, or its dimensions if unfamiliar. */
function pageSizeName(widthMm, heightMm) {
  const w = Math.min(widthMm, heightMm);
  const h = Math.max(widthMm, heightMm);
  for (const size of PAGE_SIZES) {
    if (Math.abs(size.w - w) <= PAGE_SIZE_TOLERANCE_MM && Math.abs(size.h - h) <= PAGE_SIZE_TOLERANCE_MM) {
      return size.name;
    }
  }
  return 'Custom';
}

/** Records are counted in bytes, not characters, because the server limit is bytes. */
function byteLength(text) {
  return new TextEncoder().encode(text).length;
}

/** @returns {{line:number, column:number}|null} position of a JSON.parse failure. */
function parseErrorPosition(message, text) {
  const atPos = /position (\d+)/i.exec(message);
  if (atPos) {
    const offset = Math.min(Number(atPos[1]), text.length);
    const before = text.slice(0, offset);
    const line = before.split('\n').length;
    const column = offset - before.lastIndexOf('\n');
    return { line, column };
  }
  const atLine = /line (\d+) column (\d+)/i.exec(message);
  if (atLine) return { line: Number(atLine[1]), column: Number(atLine[2]) };
  return null;
}

// ---------------------------------------------------------------------------
// 4. DOM helpers
// ---------------------------------------------------------------------------

/**
 * Looks a node up by id, falling back to a [data-el] attribute. The markup is
 * owned by another file; a missing node must degrade to "that region is absent",
 * never to a thrown TypeError that kills the whole module.
 * @returns {HTMLElement|null}
 */
function el(id) {
  return document.getElementById(id) || document.querySelector(`[data-el="${CSS.escape(id)}"]`);
}

/** @returns {HTMLElement[]} */
function all(selector, root = document) {
  return Array.prototype.slice.call(root.querySelectorAll(selector));
}

/** Creates an element with optional class, text and attributes. Text is always textContent. */
function make(tag, className, text, attrs) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (attrs) for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function makeSvg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function setText(node, text) {
  if (node) node.textContent = text === null || text === undefined ? '' : String(text);
}

function setHidden(node, hidden) {
  if (node) node.hidden = Boolean(hidden);
}

function setDisabled(node, disabled) {
  if (!node) return;
  node.disabled = Boolean(disabled);
  node.setAttribute('aria-disabled', disabled ? 'true' : 'false');
}

function on(node, type, handler, options) {
  if (node) node.addEventListener(type, handler, options);
}

/** Polite announcements for screen readers; the region is created if the markup lacks one. */
function announce(message) {
  let region = el('live-region') || el('a11y-status');
  if (!region) {
    region = make('div', 'sr-only', '', { 'aria-live': 'polite', 'aria-atomic': 'true', id: 'live-region' });
    document.body.appendChild(region);
  }
  // Re-setting identical text does not re-announce, so clear first.
  region.textContent = '';
  window.setTimeout(() => setText(region, message), 0);
}

// ---------------------------------------------------------------------------
// 5. API client
// ---------------------------------------------------------------------------

/** A non-2xx response, carrying the server's structured error envelope. */
class ApiError extends Error {
  /**
   * @param {number} status
   * @param {{code:string,message:string,request_id:string,details:Array<Object>}} payload
   * @param {number|null} retryAfterS
   */
  constructor(status, payload, retryAfterS) {
    super(payload && payload.message ? payload.message : `Request failed (HTTP ${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.code = (payload && payload.code) || 'http_error';
    this.requestId = (payload && payload.request_id) || '';
    this.details = Array.isArray(payload && payload.details) ? payload.details : [];
    this.retryAfterS = retryAfterS;
  }

  /** @returns {boolean} true when retrying unchanged input could plausibly succeed. */
  get retryable() {
    return this.status === 429 || this.status === 503 || this.status >= 500;
  }
}

/** Reading a Retry-After header, which may be seconds or an HTTP date. */
function retryAfterSeconds(response) {
  const raw = response.headers.get('Retry-After');
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds)) return Math.max(0, seconds);
  const when = Date.parse(raw);
  return Number.isFinite(when) ? Math.max(0, (when - Date.now()) / 1000) : null;
}

/** Converts any non-2xx response into an ApiError, tolerating a non-JSON body. */
async function toApiError(response) {
  let payload = null;
  try {
    const body = await response.json();
    payload = body && body.error ? body.error : null;
  } catch {
    payload = null;
  }
  if (!payload) {
    payload = { code: `http_${response.status}`, message: defaultMessageFor(response.status), request_id: '', details: [] };
  }
  return new ApiError(response.status, payload, retryAfterSeconds(response));
}

function defaultMessageFor(status) {
  if (status === 413) return 'Your records are larger than the server accepts.';
  if (status === 422) return 'The server rejected these settings.';
  if (status === 429) return 'Too many requests. Wait a moment and try again.';
  if (status === 503) return 'The server is busy. Try again in a moment.';
  if (status === 404) return 'Not found.';
  return `Request failed (HTTP ${status}).`;
}

/** The base fetch: same-origin, no credentials, always yields ApiError on failure. */
async function request(url, init) {
  let response;
  try {
    response = await fetch(url, { credentials: 'omit', cache: 'no-store', ...init });
  } catch (cause) {
    if (cause && cause.name === 'AbortError') throw cause;
    throw new ApiError(0, { code: 'network_error', message: 'Could not reach the server.', request_id: '', details: [] }, null);
  }
  if (!response.ok && response.status !== 304) throw await toApiError(response);
  return response;
}

async function requestJson(url, init) {
  const response = await request(url, {
    ...init,
    headers: { Accept: 'application/json', ...(init && init.headers) },
  });
  return response.json();
}

/** @returns {Promise<Object>} the bootstrap catalogue, limits and feature flags. */
function apiBootstrap(signal) {
  return requestJson(API.bootstrap, { method: 'GET', signal });
}

/** @returns {Promise<{template:Object, fields:string[], units:string, geometry:Geometry}>} */
function apiTemplate(id, signal) {
  return requestJson(API.template + encodeURIComponent(id), { method: 'GET', signal });
}

/**
 * @param {File} file
 * @param {string[]} fields
 * @returns {Promise<{document:string, schema:string[], record_count:number,
 *                   detected:Object, warnings:Array<Object>}>}
 */
function apiParseRecords(file, fields, signal) {
  const form = new FormData();
  form.append('file', file, file.name);
  form.append('fields', JSON.stringify(fields || []));
  return requestJson(API.parseRecords, { method: 'POST', body: form, signal });
}

/** @returns {Promise<Plan>} */
function apiPlan(renderRequest, signal) {
  return requestJson(API.plan, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(renderRequest),
    signal,
  });
}

/**
 * The preview endpoint returns raw PNG bytes, not JSON and not base64.
 * @returns {Promise<{notModified:boolean, blob:Blob|null, etag:string|null, ms:number|null}>}
 */
async function apiPreview(renderRequest, page, scale, etag, signal) {
  const headers = { 'Content-Type': 'application/json', Accept: 'image/png' };
  if (etag) headers['If-None-Match'] = etag;
  const response = await request(API.preview, {
    method: 'POST',
    headers,
    body: JSON.stringify({ ...renderRequest, page, scale }),
    signal,
  });
  if (response.status === 304) return { notModified: true, blob: null, etag, ms: null };
  const msHeader = Number(response.headers.get('X-Render-Ms'));
  return {
    notModified: false,
    blob: await response.blob(),
    etag: response.headers.get('ETag'),
    ms: Number.isFinite(msHeader) && msHeader > 0 ? msHeader : null,
  };
}

/** @returns {Promise<{blob:Blob, filename:string|null, labels:number|null, pages:number|null}>} */
async function apiPdf(renderRequest, signal) {
  const response = await request(API.pdf, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/pdf' },
    body: JSON.stringify(renderRequest),
    signal,
  });
  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(response.headers.get('Content-Disposition')),
    labels: Number(response.headers.get('X-Label-Count')) || null,
    pages: Number(response.headers.get('X-Page-Count')) || null,
  };
}

/**
 * Pulls a filename out of Content-Disposition. Anything path-like is rejected:
 * the header is server-controlled but it ends up in the user's filesystem.
 * @returns {string|null}
 */
function filenameFromDisposition(header) {
  if (!header) return null;
  let name = null;
  const extended = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (extended) {
    try {
      name = decodeURIComponent(extended[2].trim());
    } catch {
      name = null;
    }
  }
  if (!name) {
    const plain = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/i.exec(header);
    if (plain) name = (plain[1] !== undefined ? plain[1] : plain[2]).trim();
  }
  if (!name) return null;
  const base = name.split(/[\\/]/).pop().trim();
  return base && base !== '.' && base !== '..' ? base : null;
}

/** @returns {Object} the RenderRequest body shared by plan, preview and pdf. */
function buildRenderRequest() {
  return {
    template_id: state.templateId,
    layout_id: state.layoutId,
    document: state.document,
    overrides: {
      margin_top_mm: state.margins.top,
      margin_right_mm: state.margins.right,
      margin_bottom_mm: state.margins.bottom,
      margin_left_mm: state.margins.left,
    },
    page_orientation: state.orientation,
    page_rotation_deg: state.pageRotationDeg,
    text_rotation_deg: state.textRotationDeg,
    outline_slots: state.outlineSlots,
    filename: state.filename ? `${state.filename}.pdf` : null,
  };
}

// ---------------------------------------------------------------------------
// 6. Template cards and thumbnails
// ---------------------------------------------------------------------------

/**
 * Draws an accurate scale model of the sheet: page outline plus one rect per
 * grid cell, in true aspect ratio. This is the one affordance that lets someone
 * tell a 5160 from a 5163 without reading the numbers, so it is geometry-driven
 * rather than decorative.
 * @param {Geometry|null} geometry
 * @returns {SVGSVGElement}
 */
function buildThumbnail(geometry) {
  const svg = makeSvg('svg', { class: 'thumb', focusable: 'false', 'aria-hidden': 'true' });
  const pageW = geometry && Number(geometry.page_width_mm) > 0 ? Number(geometry.page_width_mm) : 0;
  const pageH = geometry && Number(geometry.page_height_mm) > 0 ? Number(geometry.page_height_mm) : 0;

  if (!pageW || !pageH) {
    // Unknown geometry (text layouts, broken templates): a neutral placeholder,
    // never a fabricated grid that would imply a size we do not know.
    svg.setAttribute('viewBox', '0 0 3 4');
    svg.setAttribute('width', String(Math.round(THUMB_LONG_EDGE_PX * 0.75)));
    svg.setAttribute('height', String(THUMB_LONG_EDGE_PX));
    svg.appendChild(makeSvg('rect', {
      x: 0.1, y: 0.1, width: 2.8, height: 3.8, rx: 0.12,
      class: 'thumb__page thumb__page--unknown',
      fill: 'none', 'stroke-dasharray': '0.3 0.25', 'vector-effect': 'non-scaling-stroke',
    }));
    return svg;
  }

  const scale = THUMB_LONG_EDGE_PX / Math.max(pageW, pageH);
  svg.setAttribute('viewBox', `0 0 ${pageW} ${pageH}`);
  svg.setAttribute('width', String(Math.max(1, Math.round(pageW * scale))));
  svg.setAttribute('height', String(Math.max(1, Math.round(pageH * scale))));
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  svg.appendChild(makeSvg('rect', {
    x: 0, y: 0, width: pageW, height: pageH,
    class: 'thumb__page', fill: 'none', 'vector-effect': 'non-scaling-stroke',
  }));

  const rows = Math.max(0, Math.floor(Number(geometry.rows) || 0));
  const cols = Math.max(0, Math.floor(Number(geometry.cols) || 0));
  const labelW = Number(geometry.label_width_mm) || 0;
  const labelH = Number(geometry.label_height_mm) || 0;
  if (!rows || !cols || labelW <= 0 || labelH <= 0 || rows * cols > THUMB_MAX_CELLS) return svg;

  const gapX = Number(geometry.gap_x_mm) || 0;
  const gapY = Number(geometry.gap_y_mm) || 0;
  const left = Number(geometry.margin_left_mm) || 0;
  const top = Number(geometry.margin_top_mm) || 0;

  const group = makeSvg('g', { class: 'thumb__cells' });
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      group.appendChild(makeSvg('rect', {
        x: left + col * (labelW + gapX),
        y: top + row * (labelH + gapY),
        width: labelW,
        height: labelH,
        class: 'thumb__cell',
        'vector-effect': 'non-scaling-stroke',
      }));
    }
  }
  svg.appendChild(group);
  return svg;
}

/** @returns {string} "3 x 10 grid - 30 per sheet" */
function gridSummary(info) {
  const g = info.geometry;
  const perSheet = Number(info.labels_per_page) || (g ? g.rows * g.cols : 0);
  if (!g) return formatCount(perSheet, 'label per sheet', 'labels per sheet');
  return `${g.cols} x ${g.rows} grid - ${formatCount(perSheet, 'per sheet', 'per sheet')}`;
}

/** @returns {string} "66.7 x 25.4 mm - Letter" */
function sizeSummary(info) {
  const g = info.geometry;
  if (!g) return 'Size defined by the label template';
  const unit = info.units === 'in' ? 'in' : 'mm';
  const w = unit === 'in' ? round(g.label_width_mm / MM_PER_INCH, IN_DECIMALS) : formatMm(g.label_width_mm);
  const h = unit === 'in' ? round(g.label_height_mm / MM_PER_INCH, IN_DECIMALS) : formatMm(g.label_height_mm);
  return `${w} x ${h} ${unit} - ${pageSizeName(g.page_width_mm, g.page_height_mm)}`;
}

const SOURCE_GROUPS = Object.freeze([
  { source: 'user', title: 'Your templates' },
  { source: 'preset', title: 'Presets' },
  { source: 'builtin', title: 'Built-in templates' },
]);

/** @returns {TemplateInfo[]} the label templates matching the current search text. */
function visibleTemplates() {
  const query = state.ui.search.trim().toLowerCase();
  if (!query) return state.templates;
  return state.templates.filter((info) => {
    const haystack = [info.id, info.name, info.description || '', ...(info.aliases || [])].join(' ').toLowerCase();
    return haystack.includes(query);
  });
}

/** Builds one radio card. Every string here is untrusted, so textContent only. */
function buildTemplateCard(info) {
  const label = make('label', 'template-card');
  label.dataset.templateId = info.id;

  const input = make('input', 'template-card__input', null, {
    type: 'radio',
    name: 'label-template',
    value: info.id,
  });
  input.checked = info.id === state.templateId;
  on(input, 'change', () => {
    if (input.checked) selectTemplate(info.id);
  });
  label.appendChild(input);

  const thumb = make('span', 'template-card__thumb');
  thumb.appendChild(buildThumbnail(info.geometry));
  label.appendChild(thumb);

  const body = make('span', 'template-card__body');
  body.appendChild(make('span', 'template-card__name', info.name || info.id));
  body.appendChild(make('span', 'template-card__meta', gridSummary(info)));
  body.appendChild(make('span', 'template-card__dims', sizeSummary(info)));

  if (Array.isArray(info.aliases) && info.aliases.length) {
    const codes = make('span', 'template-card__codes');
    for (const alias of info.aliases) codes.appendChild(make('span', 'code-chip', alias));
    body.appendChild(codes);
  }
  if (Array.isArray(info.warnings) && info.warnings.length) {
    body.appendChild(make('span', 'template-card__warning', info.warnings[0]));
  }
  label.appendChild(body);

  // The check glyph is redundant with the border colour on purpose: colour is
  // never the only channel that marks selection.
  label.appendChild(make('span', 'template-card__check', 'selected', { 'aria-hidden': 'true' }));
  return label;
}

function renderTemplateCards() {
  const list = el('template-list');
  if (!list) return;
  clear(list);

  if (state.phase === 'booting') {
    for (let i = 0; i < 3; i += 1) list.appendChild(make('div', 'template-card template-card--skeleton', '', { 'aria-hidden': 'true' }));
    return;
  }

  if (!state.templates.length) {
    const empty = make('p', 'empty-note');
    empty.appendChild(document.createTextNode('No label templates found. Add a JSON template under your template directory and reload. '));
    const link = make('a', null, 'Template JSON format', { href: '#json-format' });
    empty.appendChild(link);
    list.appendChild(empty);
    return;
  }

  const matches = visibleTemplates();
  if (!matches.length) {
    const empty = make('p', 'empty-note');
    empty.appendChild(document.createTextNode(`No templates match "${state.ui.search.trim()}". `));
    const clearBtn = make('button', 'btn btn--tertiary', 'Clear search', { type: 'button' });
    on(clearBtn, 'click', () => {
      state.ui.search = '';
      const search = el('template-search');
      if (search) search.value = '';
      renderTemplateCards();
    });
    empty.appendChild(clearBtn);
    list.appendChild(empty);
    return;
  }

  for (const group of SOURCE_GROUPS) {
    const members = matches.filter((info) => info.source === group.source);
    if (!members.length) continue;
    list.appendChild(make('h4', 'rail-subhead', group.title));
    const wrap = make('div', 'template-card-group');
    for (const info of members) wrap.appendChild(buildTemplateCard(info));
    list.appendChild(wrap);
  }

  // Anything with an unrecognised source must still be reachable.
  const known = new Set(SOURCE_GROUPS.map((g) => g.source));
  const others = matches.filter((info) => !known.has(info.source));
  if (others.length) {
    list.appendChild(make('h4', 'rail-subhead', 'Other templates'));
    const wrap = make('div', 'template-card-group');
    for (const info of others) wrap.appendChild(buildTemplateCard(info));
    list.appendChild(wrap);
  }

  const search = el('template-search');
  setHidden(search && search.closest('.rail-field') ? search.closest('.rail-field') : search,
    state.templates.length <= SEARCH_VISIBLE_THRESHOLD);
  setText(el('template-search-count'), `${matches.length} of ${state.templates.length} templates`);
}

function renderLayoutOptions() {
  const select = el('layout-select');
  if (!select) return;
  clear(select);
  const first = make('option', null, "Default (use the template's own elements)", { value: '' });
  select.appendChild(first);
  for (const info of state.layouts) {
    const option = make('option', null, info.name || info.id, { value: info.id });
    select.appendChild(option);
  }
  select.value = state.layoutId || '';
  setDisabled(select, state.phase !== 'ready');
}

function renderBrokenTemplates() {
  const host = el('broken-templates');
  if (!host) return;
  clear(host);
  if (!state.broken.length) {
    setHidden(host, true);
    return;
  }
  setHidden(host, false);
  host.setAttribute('role', 'status');
  host.appendChild(make('p', 'note-title', formatCount(state.broken.length, 'template could not be loaded', 'templates could not be loaded')));
  const list = make('ul', 'note-list');
  for (const item of state.broken) {
    const li = make('li');
    li.appendChild(make('code', null, item.id || item.path || 'unknown'));
    li.appendChild(document.createTextNode(` - ${item.message || 'unreadable'}`));
    list.appendChild(li);
  }
  host.appendChild(list);
}

/** @returns {TemplateInfo|null} */
function currentTemplate() {
  return state.templates.find((info) => info.id === state.templateId) || null;
}

/** @returns {Geometry|null} the active geometry with the orientation swap applied. */
function effectiveGeometry() {
  const info = currentTemplate();
  if (!info || !info.geometry) return null;
  const g = info.geometry;
  if (state.orientation !== 'landscape' || g.page_width_mm >= g.page_height_mm) return g;
  return { ...g, page_width_mm: g.page_height_mm, page_height_mm: g.page_width_mm };
}

async function selectTemplate(id) {
  if (!id || id === state.templateId) return;
  const previous = currentTemplate();
  const next = state.templates.find((info) => info.id === id);
  if (!next) return;

  state.templateId = id;
  state.ui.page = 0;

  // Margins belong to the template. Adopt the new ones unless the user has
  // deliberately moved them away from the previous template's defaults.
  const untouched = previous ? marginsEqual(state.margins, state.marginDefaults) : true;
  const keep = untouched ? null : { ...state.margins };
  adoptTemplateMargins(next);
  if (keep) state.margins = keep;

  renderTemplateCards();
  renderChrome();
  persistSoon();
  writeHash();
  schedulePreview(true);

  // The catalogue entry is enough to render; the detail call only refines the
  // field list, so a failure here must not break template switching.
  try {
    const detail = await apiTemplate(id);
    if (state.templateId !== id) return;
    if (detail && Array.isArray(detail.fields)) {
      next.fields = detail.fields;
      renderFieldChips();
      renderTable();
    }
  } catch {
    // Non-fatal: the plan response carries authoritative field information.
  }
}

function marginsEqual(a, b) {
  return ['top', 'right', 'bottom', 'left'].every((side) => Math.abs(a[side] - b[side]) < 1e-9);
}

function adoptTemplateMargins(info) {
  const g = info && info.geometry;
  const defaults = g
    ? { top: g.margin_top_mm, right: g.margin_right_mm, bottom: g.margin_bottom_mm, left: g.margin_left_mm }
    : { top: 0, right: 0, bottom: 0, left: 0 };
  state.marginDefaults = { ...defaults };
  state.margins = { ...defaults };
}

// ---------------------------------------------------------------------------
// 7. Settings rail
// ---------------------------------------------------------------------------

function wireSettingsRail() {
  on(el('template-search'), 'input', (event) => {
    state.ui.search = event.target.value || '';
    renderTemplateCards();
    announce(`${visibleTemplates().length} of ${state.templates.length} templates`);
  });

  on(el('layout-select'), 'change', (event) => {
    state.layoutId = event.target.value || null;
    writeHash();
    persistSoon();
    renderChrome();
    schedulePreview(true);
  });

  for (const input of all('input[name="orientation"]')) {
    on(input, 'change', () => {
      if (!input.checked) return;
      state.orientation = ORIENTATIONS.includes(input.value) ? input.value : 'portrait';
      state.ui.page = 0;
      writeHash();
      persistSoon();
      renderChrome();
      schedulePreview(true);
    });
  }

  for (const input of all('input[name="page-rotation"]')) {
    on(input, 'change', () => {
      if (!input.checked) return;
      const value = Number(input.value);
      state.pageRotationDeg = PAGE_ROTATIONS.includes(value) ? value : 0;
      writeHash();
      persistSoon();
      renderChrome();
      schedulePreview(true);
    });
  }

  on(el('outline-slots'), 'change', (event) => {
    state.outlineSlots = Boolean(event.target.checked);
    writeHash();
    persistSoon();
    schedulePreview(true);
  });

  const rotationToggle = el('text-rotation-enabled');
  const rotationInput = el('text-rotation');
  on(rotationToggle, 'change', () => {
    const enabled = Boolean(rotationToggle.checked);
    state.textRotationDeg = enabled ? Number(rotationInput && rotationInput.value) || 0 : null;
    renderChrome();
    persistSoon();
    schedulePreview(true);
  });
  on(rotationInput, 'input', debouncedPreviewFromInput(() => {
    if (!rotationToggle || rotationToggle.checked) {
      const value = Number(rotationInput.value);
      state.textRotationDeg = Number.isFinite(value) ? value : null;
    }
  }));

  wireMargins();

  const filename = el('filename');
  on(filename, 'input', () => {
    state.filename = sanitiseFilename(filename.value);
    persistSoon();
    renderChrome();
  });
  on(filename, 'blur', () => {
    if (filename.value !== state.filename) filename.value = state.filename;
  });

  on(el('margins-reset'), 'click', () => {
    state.margins = { ...state.marginDefaults };
    renderChrome();
    persistSoon();
    schedulePreview(true);
  });

  for (const input of all('input[name="margin-unit"]')) {
    on(input, 'change', () => {
      if (!input.checked) return;
      state.ui.unit = input.value === 'in' ? 'in' : 'mm';
      persistSoon();
      renderChrome();
    });
  }

  on(el('copy-cli'), 'click', copyCliCommand);
}

/**
 * The extension is fixed and rendered as an adornment, so a user-typed ".pdf",
 * a path separator or a control character must not reach Content-Disposition.
 */
function sanitiseFilename(raw) {
  const trimmed = String(raw || '').replace(/\.pdf$/i, '').trim();
  // eslint-disable-next-line no-control-regex
  const safe = trimmed.replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, '-').slice(0, 120);
  return safe || 'labels';
}

const MARGIN_SIDES = Object.freeze(['top', 'right', 'bottom', 'left']);

function wireMargins() {
  for (const side of MARGIN_SIDES) {
    const input = el(`margin-${side}`);
    if (!input) continue;
    const commit = (clampNow) => {
      const raw = Number(input.value);
      if (!Number.isFinite(raw)) {
        input.setAttribute('aria-invalid', 'true');
        return;
      }
      input.removeAttribute('aria-invalid');
      let mm = toMm(raw, state.ui.unit);
      if (clampNow) {
        // Clamping only on blur: clamping mid-typing fights the user.
        mm = clamp(mm, 0, marginMaxMm(side));
        input.value = formatLength(mm, state.ui.unit);
      }
      state.margins[side] = mm;
    };
    on(input, 'input', debouncedPreviewFromInput(() => commit(false)));
    on(input, 'change', () => {
      commit(true);
      renderChrome();
      persistSoon();
      schedulePreview(true);
    });
  }
}

/** Half the relevant page dimension: beyond that the opposite margins cross. */
function marginMaxMm(side) {
  const g = effectiveGeometry();
  if (!g) return Number.MAX_SAFE_INTEGER;
  const extent = side === 'top' || side === 'bottom' ? g.page_height_mm : g.page_width_mm;
  return Math.max(0, extent / 2);
}

/** Wraps a continuous-input handler so the state update is immediate but the render is not. */
function debouncedPreviewFromInput(apply) {
  const run = debounce(() => {
    renderChrome();
    persistSoon();
    if (state.runtime.autoRender) runPreview();
    else setStatus('stale');
  }, PREVIEW_DEBOUNCE_MS, 'previewTimer');
  return () => {
    apply();
    run();
  };
}

function renderMarginInputs() {
  const unit = state.ui.unit;
  for (const side of MARGIN_SIDES) {
    const input = el(`margin-${side}`);
    if (!input || document.activeElement === input) continue;
    input.value = formatLength(state.margins[side], unit);
    input.step = unit === 'in' ? '0.02' : '0.5';
    input.min = '0';
    const max = marginMaxMm(side);
    if (Number.isFinite(max) && max < Number.MAX_SAFE_INTEGER) {
      input.max = formatLength(max, unit);
    } else {
      input.removeAttribute('max');
    }
  }
  for (const suffix of all('[data-unit-suffix]')) setText(suffix, unit);
  for (const input of all('input[name="margin-unit"]')) input.checked = input.value === unit;
}

function renderTemplateMeta() {
  const info = currentTemplate();
  const g = effectiveGeometry();
  if (!info || !g) {
    setText(el('template-meta-page'), '');
    setText(el('template-meta-grid'), '');
    return;
  }
  setText(el('template-meta-page'), `${pageSizeName(g.page_width_mm, g.page_height_mm)} - ${formatMm(g.page_width_mm)} x ${formatMm(g.page_height_mm)} mm`);
  setText(el('template-meta-grid'), `${g.cols} x ${g.rows} grid - ${formatMm(g.label_width_mm)} x ${formatMm(g.label_height_mm)} mm - ${info.labels_per_page} per page`);
}

/** Composes the equivalent CLI invocation, omitting every flag left at its default. */
function buildCliCommand() {
  const parts = ['label-sheet', 'generate', state.templateId || '<template>', `${state.filename || 'labels'}.pdf`];
  if (state.layoutId) parts.push('--layout-template', state.layoutId);
  if (recordCount() > 0) parts.push('--records', 'records.json');
  if (state.orientation !== 'portrait') parts.push('--orientation', state.orientation);
  if (state.pageRotationDeg !== 0) parts.push('--page-rotation', String(state.pageRotationDeg));
  if (state.textRotationDeg !== null) parts.push('--text-rotation', String(state.textRotationDeg));
  if (state.outlineSlots) parts.push('--draw-border');
  for (const side of MARGIN_SIDES) {
    if (Math.abs(state.margins[side] - state.marginDefaults[side]) > 1e-9) {
      parts.push(`--margin-${side}`, formatLength(state.margins[side], 'mm'));
    }
  }
  return parts.join(' ');
}

async function copyCliCommand() {
  const button = el('copy-cli');
  const command = buildCliCommand();
  const copied = await copyText(command);
  if (!button) return;
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  setText(button, copied ? 'Copied' : 'Copy failed');
  announce(copied ? 'CLI command copied to the clipboard.' : 'Could not copy the command.');
  window.setTimeout(() => setText(button, original), COPIED_FLASH_MS);
}

/** Clipboard API where available, with the legacy path for non-secure contexts. */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path rather than failing outright.
    }
  }
  const scratch = make('textarea');
  scratch.value = text;
  scratch.setAttribute('readonly', 'readonly');
  scratch.style.position = 'fixed';
  scratch.style.opacity = '0';
  document.body.appendChild(scratch);
  scratch.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  document.body.removeChild(scratch);
  return ok;
}

// ---------------------------------------------------------------------------
// 8. Records editor
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} ParsedDocument
 * @property {boolean} ok
 * @property {string[]} schema
 * @property {Array<Object>} records
 * @property {{message:string, line:number|null, column:number|null}|null} error
 */

/** @returns {ParsedDocument} the current document, parsed and normalised. */
function parseDocument(text) {
  const source = String(text || '').trim();
  if (!source) return { ok: true, schema: [], records: [], error: null };

  let data;
  try {
    data = JSON.parse(source);
  } catch (cause) {
    const position = parseErrorPosition(cause.message || '', source);
    return {
      ok: false,
      schema: [],
      records: [],
      error: {
        message: cause.message || 'The records are not valid JSON.',
        line: position ? position.line : null,
        column: position ? position.column : null,
      },
    };
  }

  // A bare array is the legacy on-disk form and is normalised to the object form.
  if (Array.isArray(data)) {
    const records = data.filter(isPlainRecord);
    if (records.length !== data.length) {
      return { ok: false, schema: [], records: [], error: { message: 'Every record must be a JSON object.', line: null, column: null } };
    }
    return { ok: true, schema: unionKeys(records), records, error: null };
  }

  if (!isPlainRecord(data)) {
    return { ok: false, schema: [], records: [], error: { message: 'Expected an object with "schema" and "records", or an array of records.', line: null, column: null } };
  }

  const rawRecords = Array.isArray(data.records) ? data.records : [];
  if (!rawRecords.every(isPlainRecord)) {
    return { ok: false, schema: [], records: [], error: { message: '"records" must be an array of JSON objects.', line: null, column: null } };
  }
  const schema = Array.isArray(data.schema) ? data.schema.filter((k) => typeof k === 'string') : unionKeys(rawRecords);
  return { ok: true, schema: schema.length ? schema : unionKeys(rawRecords), records: rawRecords, error: null };
}

function isPlainRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function unionKeys(records) {
  const keys = [];
  const seen = new Set();
  for (const record of records) {
    for (const key of Object.keys(record)) {
      if (!seen.has(key)) {
        seen.add(key);
        keys.push(key);
      }
    }
  }
  return keys;
}

/**
 * The document a fresh session opens with: the active template's shipped
 * example records if it has them, otherwise an empty document with the right
 * schema so the table editor already has the correct columns.
 * @returns {string}
 */
function startingDocument() {
  const info = currentTemplate();
  if (info && typeof info.example_document === 'string' && info.example_document.trim()) {
    return info.example_document;
  }
  return serialiseDocument(templateFields(), []);
}

function serialiseDocument(schema, records) {
  return JSON.stringify({ schema, records }, null, 2);
}

function recordCount() {
  const parsed = parseDocument(state.document);
  return parsed.ok ? parsed.records.length : 0;
}

/** @returns {string[]} the fields the active template expects. */
function templateFields() {
  if (state.plan && Array.isArray(state.plan.fields)) return state.plan.fields;
  const info = currentTemplate();
  return info && Array.isArray(info.fields) ? info.fields : [];
}

/** Columns are the data's own keys first, then any template field the data lacks. */
function tableColumns(parsed) {
  const columns = parsed.schema.slice();
  for (const field of templateFields()) if (!columns.includes(field)) columns.push(field);
  return columns;
}

function setDocument(text, options) {
  const opts = options || {};
  state.document = String(text);
  // Warnings describe the previous document; a new one invalidates them.
  if (!opts.keepMessages) state.runtime.messages = [];
  if (!opts.fromJsonEditor) syncJsonEditor();
  if (!opts.fromTable) renderTable();
  renderRecordsChrome();
  persistDocumentSoon();
  if (opts.silent !== true) schedulePreview(Boolean(opts.immediate));
}

function wireRecordsEditor() {
  on(el('tab-table'), 'click', () => setTab('table'));
  on(el('tab-json'), 'click', () => setTab('json'));

  const toggle = el('records-toggle');
  on(toggle, 'click', () => {
    state.ui.recordsOpen = !state.ui.recordsOpen;
    renderRecordsChrome();
    persistSoon();
  });

  const textarea = el('records-json');
  on(textarea, 'input', () => {
    state.runtime.escapedTextarea = false;
    setDocument(textarea.value, { fromJsonEditor: true });
    renderGutter();
  });
  on(textarea, 'scroll', () => {
    const gutter = el('records-gutter');
    if (gutter) gutter.scrollTop = textarea.scrollTop;
  });
  on(textarea, 'keydown', handleJsonKeydown);

  on(el('add-row'), 'click', () => addRecord(true));
  on(el('clear-records'), 'click', clearRecords);
  on(el('format-json'), 'click', formatJson);

  const fileInput = el('import-file');
  on(el('import-records'), 'click', () => {
    if (fileInput) fileInput.click();
  });
  on(fileInput, 'change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) importFile(file);
    fileInput.value = '';
  });

  wireDropTarget(el('records-panel'));
  wireDropTarget(el('desk'));
  wireRecordsResizer();
}

function setTab(tab) {
  const parsed = parseDocument(state.document);
  if (tab === 'table' && !parsed.ok) {
    // The table cannot project a document it cannot parse; say so in the strip,
    // never only in a tooltip.
    announce('Fix the JSON error to use the table view.');
    renderRecordsChrome();
    return;
  }
  state.ui.tab = tab === 'json' ? 'json' : 'table';
  renderRecordsChrome();
  persistSoon();
}

function handleJsonKeydown(event) {
  const textarea = event.currentTarget;
  if (event.key === 'Escape') {
    // Esc then Tab must leave the field, so a keyboard user is never trapped.
    state.runtime.escapedTextarea = true;
    return;
  }
  if (event.key !== 'Tab' || event.ctrlKey || event.metaKey || event.altKey) return;
  if (state.runtime.escapedTextarea) {
    state.runtime.escapedTextarea = false;
    return;
  }
  event.preventDefault();
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const indent = '  ';
  textarea.value = `${textarea.value.slice(0, start)}${indent}${textarea.value.slice(end)}`;
  textarea.selectionStart = start + indent.length;
  textarea.selectionEnd = start + indent.length;
  setDocument(textarea.value, { fromJsonEditor: true });
  renderGutter();
}

function syncJsonEditor() {
  const textarea = el('records-json');
  if (!textarea || document.activeElement === textarea) return;
  textarea.value = state.document;
  renderGutter();
}

function renderGutter() {
  const gutter = el('records-gutter');
  const textarea = el('records-json');
  if (!gutter || !textarea) return;
  const lines = textarea.value.split('\n').length;
  const current = Number(gutter.dataset.lines || '0');
  if (current === lines) return;
  gutter.dataset.lines = String(lines);
  clear(gutter);
  for (let i = 1; i <= lines; i += 1) gutter.appendChild(make('span', 'gutter__line', i));
  gutter.scrollTop = textarea.scrollTop;
}

function formatJson() {
  const parsed = parseDocument(state.document);
  if (!parsed.ok) {
    // Formatting must never destroy text the user is mid-way through fixing.
    renderRecordsChrome();
    announce('Could not format: the records are not valid JSON.');
    return;
  }
  setDocument(serialiseDocument(parsed.schema, parsed.records), { immediate: true });
  announce('Formatted the records document.');
}

const commitTable = debounce(() => {
  const table = el('records-table');
  if (!table) return;
  const parsed = parseDocument(state.document);
  if (!parsed.ok) return;
  const columns = tableColumns(parsed);
  const records = all('tbody tr', table).map((row) => {
    const record = {};
    for (const input of all('input[data-field]', row)) {
      // Values are never coerced: "01" must stay the string "01".
      record[input.dataset.field] = input.value;
    }
    for (const key of columns) if (!(key in record)) record[key] = '';
    return record;
  });
  setDocument(serialiseDocument(columns, records), { fromTable: true });
}, TABLE_COMMIT_MS, 'tableTimer');

function renderTable() {
  const table = el('records-table');
  if (!table) return;
  const parsed = parseDocument(state.document);

  const head = table.tHead || table.createTHead();
  const body = table.tBodies[0] || table.createTBody();
  clear(head);
  clear(body);

  if (!parsed.ok) {
    setDisabled(el('tab-table'), true);
    return;
  }
  setDisabled(el('tab-table'), false);

  const columns = tableColumns(parsed);
  const expected = new Set(templateFields());

  const headRow = head.insertRow();
  headRow.appendChild(make('th', 'col-num', '#', { scope: 'col' }));
  for (const column of columns) {
    const th = make('th', 'col-field', null, { scope: 'col' });
    th.appendChild(make('span', 'col-field__name', column));
    if (!expected.has(column)) th.appendChild(make('span', 'tag tag--unused', 'unused'));
    headRow.appendChild(th);
  }
  headRow.appendChild(make('th', 'col-actions', 'Delete', { scope: 'col' }));

  // A table with no rows offers one blank row rather than an empty grid.
  const rows = parsed.records.length ? parsed.records : [{}];
  rows.forEach((record, index) => body.appendChild(buildTableRow(record, index, columns)));
}

function buildTableRow(record, index, columns) {
  const tr = document.createElement('tr');
  tr.appendChild(make('th', 'col-num', index + 1, { scope: 'row' }));
  for (const column of columns) {
    const td = make('td');
    const input = make('input', 'cell-input', null, { type: 'text', 'aria-label': `${column}, record ${index + 1}` });
    input.dataset.field = column;
    input.dataset.row = String(index);
    const value = record[column];
    input.value = value === undefined || value === null ? '' : String(value);
    on(input, 'input', commitTable);
    on(input, 'keydown', handleCellKeydown);
    on(input, 'paste', handleCellPaste);
    td.appendChild(input);
    tr.appendChild(td);
  }
  const actions = make('td', 'col-actions');
  const del = make('button', 'btn btn--icon', 'Delete', { type: 'button', 'aria-label': `Delete record ${index + 1}` });
  on(del, 'click', () => deleteRecord(index));
  actions.appendChild(del);
  tr.appendChild(actions);
  return tr;
}

function handleCellKeydown(event) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  const input = event.currentTarget;
  const row = Number(input.dataset.row);
  const field = input.dataset.field;
  const table = el('records-table');
  if (!table) return;
  const next = table.querySelector(`input[data-row="${row + 1}"][data-field="${CSS.escape(field)}"]`);
  if (next) {
    next.focus();
    next.select();
    return;
  }
  addRecord(true);
}

/**
 * Pasting a block of TSV/CSV into a cell fills rightward and downward, which is
 * how every spreadsheet behaves and how people actually move data in here.
 */
function handleCellPaste(event) {
  const text = event.clipboardData ? event.clipboardData.getData('text/plain') : '';
  if (!text || !/[\t\n]/.test(text)) return;
  event.preventDefault();

  const input = event.currentTarget;
  const startRow = Number(input.dataset.row);
  const parsed = parseDocument(state.document);
  if (!parsed.ok) return;
  const columns = tableColumns(parsed);
  const startCol = Math.max(0, columns.indexOf(input.dataset.field));

  const grid = text.replace(/\r\n?/g, '\n').replace(/\n$/, '').split('\n').map(splitDelimited);
  const records = parsed.records.length ? parsed.records.map((r) => ({ ...r })) : [{}];

  grid.forEach((cells, rowOffset) => {
    const rowIndex = startRow + rowOffset;
    while (records.length <= rowIndex) records.push({});
    cells.forEach((cell, colOffset) => {
      const column = columns[startCol + colOffset];
      if (column) records[rowIndex][column] = cell;
    });
  });

  setDocument(serialiseDocument(columns, records), { immediate: true });
  announce(`Pasted ${formatCount(grid.length, 'record', 'records')}.`);
}

/** Splits a pasted line on tabs, falling back to commas when there are none. */
function splitDelimited(line) {
  if (line.includes('\t')) return line.split('\t');
  return line.split(',').map((part) => part.trim());
}

function addRecord(focusFirst) {
  const parsed = parseDocument(state.document);
  if (!parsed.ok) return;
  const columns = tableColumns(parsed);
  const blank = {};
  for (const column of columns) blank[column] = '';
  const records = parsed.records.concat([blank]);
  setDocument(serialiseDocument(columns, records), { immediate: true });
  if (!focusFirst) return;
  const table = el('records-table');
  const cell = table && table.querySelector(`input[data-row="${records.length - 1}"]`);
  if (cell) cell.focus();
}

function deleteRecord(index) {
  const parsed = parseDocument(state.document);
  if (!parsed.ok) return;
  const previous = state.document;
  const columns = tableColumns(parsed);
  const records = parsed.records.slice();
  records.splice(index, 1);
  setDocument(serialiseDocument(columns, records), { immediate: true });
  announce(`Deleted record ${index + 1}.`);
  offerUndo(`Deleted record ${index + 1}.`, previous, UNDO_DELETE_MS);
}

function clearRecords() {
  const parsed = parseDocument(state.document);
  const previous = state.document;
  const columns = parsed.ok ? tableColumns(parsed) : templateFields();
  setDocument(serialiseDocument(columns, []), { immediate: true });
  announce('Cleared all records.');
  offerUndo('Cleared all records.', previous, UNDO_DELETE_MS);
}

/** A time-boxed undo so a destructive click is recoverable without a dialog. */
function offerUndo(message, previousDocument, ttlMs) {
  showToast(message, { actionLabel: 'Undo', ttlMs, onAction: () => setDocument(previousDocument, { immediate: true }) });
}

// --- Import ---------------------------------------------------------------

function wireDropTarget(node) {
  if (!node) return;
  const over = (event) => {
    if (!event.dataTransfer || !Array.prototype.includes.call(event.dataTransfer.types || [], 'Files')) return;
    event.preventDefault();
    node.classList.add('is-drop-target');
  };
  on(node, 'dragover', over);
  on(node, 'dragenter', over);
  on(node, 'dragleave', () => node.classList.remove('is-drop-target'));
  on(node, 'drop', (event) => {
    if (!event.dataTransfer || !event.dataTransfer.files || !event.dataTransfer.files.length) return;
    event.preventDefault();
    node.classList.remove('is-drop-target');
    importFile(event.dataTransfer.files[0]);
  });
}

const IMPORT_EXTENSIONS = Object.freeze(['.csv', '.json', '.tsv']);

async function importFile(file) {
  const maxBytes = Number(state.limits.max_upload_bytes) || FALLBACK_MAX_UPLOAD_BYTES;
  const name = String(file.name || '');
  const dot = name.lastIndexOf('.');
  const extension = dot >= 0 ? name.slice(dot).toLowerCase() : '';
  const allowed = IMPORT_EXTENSIONS.concat(state.features && state.features.pdf_import ? ['.pdf'] : []);

  if (!allowed.includes(extension)) {
    setImportError(`Unsupported file type "${extension || name}". Accepted: ${allowed.join(', ')}.`);
    return;
  }
  if (file.size > maxBytes) {
    setImportError(`That file is ${Math.round(file.size / 1024)} KB. The server accepts up to ${Math.round(maxBytes / 1024)} KB.`);
    return;
  }

  setImportError(null);
  const previous = state.document;
  try {
    const result = await apiParseRecords(file, templateFields());
    setDocument(result.document, { immediate: true });
    state.runtime.messages = normaliseWarnings(result.warnings);
    renderValidationStrip();
    const detected = result.detected && result.detected.format ? ` (${result.detected.format})` : '';
    announce(`Loaded ${formatCount(result.record_count, 'record', 'records')} from ${name}${detected}.`);
    offerUndo(`Loaded ${formatCount(result.record_count, 'record', 'records')} from ${name}.`, previous, UNDO_IMPORT_MS);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    reportError(error, 'import');
  }
}

function setImportError(message) {
  const host = el('import-error');
  if (!host) {
    if (message) showToast(message, {});
    return;
  }
  setText(host, message || '');
  setHidden(host, !message);
  if (message) host.setAttribute('role', 'alert');
}

function normaliseWarnings(warnings) {
  if (!Array.isArray(warnings)) return [];
  return warnings
    .filter((w) => w && typeof w.message === 'string')
    .map((w) => ({ severity: 'warning', message: w.message, row: Number.isFinite(w.row) ? w.row : null, code: w.code || '' }));
}

// --- Records chrome -------------------------------------------------------

function renderFieldChips() {
  const host = el('field-chips');
  if (!host) return;
  clear(host);

  const fields = templateFields();
  const missing = new Set((state.plan && state.plan.missing_fields) || []);
  const extra = (state.plan && state.plan.extra_fields) || [];

  for (const field of fields) {
    const isMissing = missing.has(field);
    const chip = make('button', `chip ${isMissing ? 'chip--missing' : 'chip--present'}`, null, { type: 'button' });
    chip.appendChild(make('span', 'chip__dot', isMissing ? '!' : '', { 'aria-hidden': 'true' }));
    chip.appendChild(make('span', 'chip__label', field));
    chip.setAttribute('aria-label', isMissing ? `${field} - missing from your data` : field);
    on(chip, 'click', () => focusColumn(field));
    host.appendChild(chip);
  }

  if (extra.length) {
    const group = make('span', 'chip-group chip-group--unused');
    group.appendChild(make('span', 'chip-group__label', 'unused:'));
    for (const field of extra) {
      const chip = make('button', 'chip chip--unused', field, { type: 'button' });
      chip.setAttribute('aria-label', `${field} - present in your data, not used by this template`);
      on(chip, 'click', () => focusColumn(field));
      group.appendChild(chip);
    }
    host.appendChild(group);
  }
}

function focusColumn(field) {
  if (state.ui.tab !== 'table') setTab('table');
  const table = el('records-table');
  const cell = table && table.querySelector(`input[data-field="${CSS.escape(field)}"]`);
  if (cell) {
    cell.focus();
    cell.select();
  }
}

function renderValidationStrip() {
  const strip = el('validation-strip');
  if (!strip) return;
  clear(strip);

  const issues = collectIssues();
  setHidden(strip, issues.length === 0);
  if (!issues.length) return;

  for (const issue of issues) {
    const line = make('p', `validation-line validation-line--${issue.severity}`);
    line.appendChild(make('span', 'validation-line__glyph', issue.severity === 'error' ? 'Error' : 'Warning', { 'aria-hidden': 'false' }));
    line.appendChild(make('span', 'validation-line__text', issue.message));
    if (issue.jump) {
      const jump = make('button', 'btn btn--link', issue.jump.label, { type: 'button' });
      on(jump, 'click', issue.jump.action);
      line.appendChild(jump);
    }
    strip.appendChild(line);
  }
}

/** @returns {Array<{severity:string, message:string, jump:Object|null}>} */
function collectIssues() {
  const issues = [];
  const parsed = parseDocument(state.document);

  if (!parsed.ok && parsed.error) {
    const where = parsed.error.line ? ` (line ${parsed.error.line}, column ${parsed.error.column})` : '';
    issues.push({
      severity: 'error',
      message: `${parsed.error.message}${where}`,
      jump: parsed.error.line ? { label: `Line ${parsed.error.line}`, action: () => jumpToLine(parsed.error.line) } : null,
    });
    issues.push({ severity: 'error', message: 'Fix the JSON error to use the table view.', jump: null });
  }

  const plan = state.plan;
  if (plan) {
    for (const field of plan.missing_fields || []) {
      issues.push({
        severity: 'warning',
        message: `Your data has no "${field}" column - those slots print blank.`,
        jump: { label: `Add ${field}`, action: () => focusColumn(field) },
      });
    }
    for (const issue of (plan.geometry && plan.geometry.errors) || []) {
      issues.push({ severity: 'error', message: issue.message || 'The grid does not fit this page.', jump: null });
    }
    for (const issue of (plan.geometry && plan.geometry.warnings) || []) {
      issues.push({ severity: 'warning', message: issue.message || 'Geometry warning.', jump: null });
    }
    for (const warning of plan.record_warnings || []) {
      const row = Number.isFinite(warning.row) ? warning.row : null;
      issues.push({
        severity: 'warning',
        message: warning.message || 'Record warning.',
        jump: row === null ? null : { label: `Row ${row + 1}`, action: () => focusRow(row) },
      });
    }
  }

  for (const message of state.runtime.messages) {
    issues.push({ severity: message.severity, message: message.message, jump: null });
  }
  return issues;
}

function jumpToLine(line) {
  setTab('json');
  const textarea = el('records-json');
  if (!textarea) return;
  const lines = textarea.value.split('\n');
  let offset = 0;
  for (let i = 0; i < Math.min(line - 1, lines.length); i += 1) offset += lines[i].length + 1;
  textarea.focus();
  textarea.setSelectionRange(offset, offset + (lines[line - 1] ? lines[line - 1].length : 0));
}

function focusRow(row) {
  if (state.ui.tab !== 'table') setTab('table');
  const table = el('records-table');
  const cell = table && table.querySelector(`input[data-row="${row}"]`);
  if (cell) cell.focus();
}

function renderRecordsChrome() {
  const parsed = parseDocument(state.document);
  const panel = el('records-panel');

  // The table cannot exist over an unparseable document, so the JSON tab is forced.
  if (!parsed.ok && state.ui.tab !== 'json') state.ui.tab = 'json';

  const tableTab = el('tab-table');
  const jsonTab = el('tab-json');
  const tablePanel = el('panel-table');
  const jsonPanel = el('panel-json');
  const showTable = state.ui.tab === 'table' && parsed.ok;

  if (tableTab) tableTab.setAttribute('aria-selected', showTable ? 'true' : 'false');
  if (jsonTab) jsonTab.setAttribute('aria-selected', showTable ? 'false' : 'true');
  setDisabled(tableTab, !parsed.ok);
  setHidden(tablePanel, !showTable);
  setHidden(jsonPanel, showTable);

  const textarea = el('records-json');
  if (textarea) textarea.setAttribute('aria-invalid', parsed.ok ? 'false' : 'true');

  const fields = templateFields();
  setText(el('records-summary'), `${formatCount(parsed.records.length, 'label', 'labels')} - ${formatCount(fields.length, 'field', 'fields')}`);

  const chip = el('records-status');
  if (chip) {
    const missing = (state.plan && state.plan.missing_fields) || [];
    if (!parsed.ok) {
      chip.className = 'status-pill status-pill--error';
      setText(chip, 'JSON error');
    } else if (missing.length) {
      chip.className = 'status-pill status-pill--warn';
      setText(chip, `missing: ${missing.join(', ')}`);
    } else {
      chip.className = 'status-pill status-pill--ok';
      setText(chip, 'valid');
    }
  }

  if (panel) {
    panel.classList.toggle('is-open', state.ui.recordsOpen);
    panel.style.setProperty('--records-height', `${state.ui.recordsHeight}px`);
  }
  const toggle = el('records-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', state.ui.recordsOpen ? 'true' : 'false');
    toggle.setAttribute('aria-label', state.ui.recordsOpen ? 'Collapse records' : 'Expand records');
  }
  setHidden(el('records-body'), !state.ui.recordsOpen);

  // Templates with no fields need no records at all; say so instead of showing
  // an editor that cannot affect the output.
  setHidden(el('records-no-fields-note'), fields.length > 0);

  renderFieldChips();
  renderValidationStrip();
}

function wireRecordsResizer() {
  const handle = el('records-resizer');
  if (!handle) return;

  const maxHeight = () => Math.max(RECORDS_MIN_H, Math.round(window.innerHeight * RECORDS_MAX_VIEWPORT_FRACTION));
  const apply = (height) => {
    state.ui.recordsHeight = clamp(Math.round(height), RECORDS_MIN_H, maxHeight());
    handle.setAttribute('aria-valuenow', String(state.ui.recordsHeight));
    handle.setAttribute('aria-valuemin', String(RECORDS_MIN_H));
    handle.setAttribute('aria-valuemax', String(maxHeight()));
    const panel = el('records-panel');
    if (panel) panel.style.setProperty('--records-height', `${state.ui.recordsHeight}px`);
    persistSoon();
  };

  on(handle, 'pointerdown', (event) => {
    handle.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startH = state.ui.recordsHeight;
    const move = (moveEvent) => apply(startH + (startY - moveEvent.clientY));
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
  });

  // Drag is never the only way to resize.
  on(handle, 'keydown', (event) => {
    const step = event.shiftKey ? RECORDS_BIG_STEP_H : RECORDS_STEP_H;
    if (event.key === 'ArrowUp') apply(state.ui.recordsHeight + step);
    else if (event.key === 'ArrowDown') apply(state.ui.recordsHeight - step);
    else if (event.key === 'Home') apply(RECORDS_MIN_H);
    else if (event.key === 'End') apply(maxHeight());
    else return;
    event.preventDefault();
  });

  apply(state.ui.recordsHeight);
}

// ---------------------------------------------------------------------------
// 9. Preview loop
// ---------------------------------------------------------------------------

const schedulePreviewDebounced = debounce(() => runPreview(), PREVIEW_DEBOUNCE_MS, 'previewTimer');

/** @param {boolean} immediate true for discrete inputs, where the user has finished deciding. */
function schedulePreview(immediate) {
  if (state.phase !== 'ready') return;
  if (!state.runtime.autoRender) {
    setStatus('stale');
    return;
  }
  if (immediate) {
    window.clearTimeout(state.runtime.previewTimer);
    runPreview();
    return;
  }
  schedulePreviewDebounced();
}

/** @returns {number} the device-appropriate render scale, quantised and clamped. */
function previewScale() {
  const figure = el('sheet');
  const cssWidth = figure ? figure.clientWidth || PREVIEW_SCALE_REFERENCE_PX : PREVIEW_SCALE_REFERENCE_PX;
  const dpr = window.devicePixelRatio || 1;
  const raw = dpr * (cssWidth / PREVIEW_SCALE_REFERENCE_PX);
  const quantised = Math.round(raw / PREVIEW_SCALE_STEP) * PREVIEW_SCALE_STEP;
  const lo = Number(state.limits.preview_scale_min) || 0.5;
  const hi = Number(state.limits.preview_scale_max) || 3;
  return clamp(quantised || Number(state.limits.preview_scale_default) || 1.5, lo, hi);
}

async function runPreview() {
  if (state.phase !== 'ready' || !state.templateId) return;

  state.runtime.seq += 1;
  const mySeq = state.runtime.seq;

  // An in-flight render is worthless the moment the input changes again.
  if (state.runtime.controller) state.runtime.controller.abort();
  const controller = new AbortController();
  state.runtime.controller = controller;

  const parsed = parseDocument(state.document);
  if (!parsed.ok) {
    setStatus('attention');
    renderRecordsChrome();
    renderExportState();
    return;
  }

  setStatus('rendering');
  startProgress();
  const started = performance.now();
  const requestBody = buildRenderRequest();

  try {
    const plan = await apiPlan(requestBody, controller.signal);
    if (mySeq !== state.runtime.seq) return;
    state.plan = plan;
    state.ui.page = clamp(state.ui.page, 0, Math.max(0, (plan.pages || 1) - 1));
    renderChrome();

    const geometryOk = !plan.geometry || plan.geometry.ok !== false;
    if (!geometryOk) {
      showGeometryError(plan.geometry.errors || []);
      setStatus('attention');
      return;
    }
    clearSheetOverlay();

    if ((plan.labels || 0) === 0 && templateFields().length > 0) {
      showEmptySheet();
      setStatus('idle');
      return;
    }

    await loadPreviewImage(requestBody, mySeq, controller.signal);
    if (mySeq !== state.runtime.seq) return;
    state.runtime.lastMs = Math.round(performance.now() - started);
    state.runtime.failures = 0;
    setStatus('ok');
    announce(`Preview updated. ${formatCount(plan.labels, 'label', 'labels')} across ${formatCount(plan.pages, 'page', 'pages')}.`);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    if (mySeq !== state.runtime.seq) return;
    state.runtime.failures += 1;
    setStatus('attention');
    reportError(error, 'preview');
  } finally {
    if (mySeq === state.runtime.seq) {
      stopProgress();
      state.runtime.controller = null;
      renderExportState();
    }
  }
}

async function loadPreviewImage(requestBody, mySeq, signal) {
  const img = el('preview-image');
  const figure = el('sheet');
  if (!img) return;

  const scale = previewScale();
  const key = JSON.stringify([requestBody, state.ui.page, scale]);
  const etag = key === state.runtime.previewKey ? state.runtime.previewEtag : null;

  if (figure) figure.setAttribute('aria-busy', 'true');
  img.classList.add('is-stale');

  const result = await apiPreview(requestBody, state.ui.page, scale, etag, signal);
  if (mySeq !== state.runtime.seq) return;

  state.runtime.previewKey = key;
  if (result.notModified) {
    img.classList.remove('is-stale');
    if (figure) figure.removeAttribute('aria-busy');
    return;
  }
  state.runtime.previewEtag = result.etag;

  const url = URL.createObjectURL(result.blob);
  // Decode off-screen first so the visible sheet is never blank between frames.
  const probe = new Image();
  probe.src = url;
  try {
    if (typeof probe.decode === 'function') await probe.decode();
  } catch {
    // A decode failure still leaves a usable URL for the <img> to attempt.
  }
  if (mySeq !== state.runtime.seq) {
    URL.revokeObjectURL(url);
    return;
  }

  const previous = state.runtime.objectUrl;
  img.src = url;
  img.alt = describeSheet();
  img.classList.remove('is-stale');
  state.runtime.objectUrl = url;
  if (previous) URL.revokeObjectURL(previous);
  if (figure) figure.removeAttribute('aria-busy');
  setHidden(img, false);
}

/** @returns {string} the caption and the image's alt text - one description, two homes. */
function describeSheet() {
  const g = effectiveGeometry();
  const plan = state.plan;
  const parts = [];
  if (g) {
    parts.push(pageSizeName(g.page_width_mm, g.page_height_mm));
    parts.push(state.orientation === 'landscape' ? 'Landscape' : 'Portrait');
    parts.push(`${g.cols} x ${g.rows}`);
    parts.push(`${formatMm(g.label_width_mm)} x ${formatMm(g.label_height_mm)} mm labels`);
  }
  if (plan && plan.pages) parts.push(`page ${state.ui.page + 1} of ${plan.pages}`);
  return parts.join(' - ') || 'Sheet preview';
}

function startProgress() {
  window.clearTimeout(state.runtime.progressTimer);
  window.clearTimeout(state.runtime.coldStartTimer);
  const bar = el('render-progress');
  state.runtime.progressTimer = window.setTimeout(() => setHidden(bar, false), PROGRESS_DELAY_MS);
  state.runtime.coldStartTimer = window.setTimeout(() => {
    if (state.runtime.status === 'rendering') setStatus('rendering', 'the first render can take a few seconds');
  }, COLD_START_NOTICE_MS);
}

function stopProgress() {
  window.clearTimeout(state.runtime.progressTimer);
  window.clearTimeout(state.runtime.coldStartTimer);
  setHidden(el('render-progress'), true);
}

/** @param {"idle"|"rendering"|"ok"|"attention"|"stale"} kind */
function setStatus(kind, note) {
  state.runtime.status = kind;
  const chip = el('status-chip');
  if (!chip) return;
  chip.dataset.status = kind;
  if (kind === 'rendering') setText(chip, note ? `Rendering... (${note})` : 'Rendering...');
  else if (kind === 'ok') setText(chip, state.runtime.lastMs === null ? 'Up to date' : `Up to date - ${state.runtime.lastMs} ms`);
  else if (kind === 'attention') setText(chip, 'Needs attention');
  else if (kind === 'stale') setText(chip, 'Changes not rendered');
  else setText(chip, 'Ready');
}

/** Replaces the sheet in place with an error card of the same shape. */
function showGeometryError(errors) {
  const host = el('sheet-overlay');
  const img = el('preview-image');
  if (img) setHidden(img, true);
  if (!host) return;
  clear(host);
  setHidden(host, false);
  host.setAttribute('role', 'alert');
  host.appendChild(make('p', 'sheet-overlay__title', 'Cannot lay out this sheet'));
  for (const issue of errors.slice(0, 3)) {
    host.appendChild(make('p', 'sheet-overlay__message', issue.message || 'The grid does not fit the page.'));
  }
  const reset = make('button', 'btn btn--primary', 'Reset margins', { type: 'button' });
  on(reset, 'click', () => {
    state.margins = { ...state.marginDefaults };
    renderChrome();
    schedulePreview(true);
  });
  host.appendChild(reset);
}

function showEmptySheet() {
  const host = el('sheet-overlay');
  const img = el('preview-image');
  if (img) setHidden(img, true);
  if (!host) return;
  clear(host);
  setHidden(host, false);
  host.removeAttribute('role');
  host.appendChild(make('p', 'sheet-overlay__title', 'Add records to see your sheet'));
  const add = make('button', 'btn btn--tertiary', 'Add a record', { type: 'button' });
  on(add, 'click', () => {
    state.ui.recordsOpen = true;
    renderRecordsChrome();
    addRecord(true);
  });
  const importBtn = make('button', 'btn btn--tertiary', 'Import a CSV', { type: 'button' });
  on(importBtn, 'click', () => {
    const fileInput = el('import-file');
    if (fileInput) fileInput.click();
  });
  const actions = make('div', 'sheet-overlay__actions');
  actions.appendChild(add);
  actions.appendChild(importBtn);
  host.appendChild(actions);
}

function clearSheetOverlay() {
  const host = el('sheet-overlay');
  if (host) {
    clear(host);
    setHidden(host, true);
    host.removeAttribute('role');
  }
  setHidden(el('preview-image'), false);
}

function wirePreviewControls() {
  on(el('page-prev'), 'click', () => stepPage(-1));
  on(el('page-next'), 'click', () => stepPage(1));

  const showMargins = el('show-margins');
  on(showMargins, 'change', () => {
    state.ui.showMargins = Boolean(showMargins.checked);
    renderSheetFrame();
    persistSoon();
  });

  const dimSheet = el('dim-sheet');
  on(dimSheet, 'change', () => {
    state.ui.dimSheet = Boolean(dimSheet.checked);
    renderSheetFrame();
    persistSoon();
  });

  on(el('preview-retry'), 'click', () => {
    state.runtime.failures = 0;
    schedulePreview(true);
  });

  const renderNow = el('render-now');
  // Save-data mode turns auto-render off; this button is the manual trigger.
  if (renderNow) on(renderNow, 'click', () => runPreview());

  // Re-rendering at the new CSS width keeps the PNG crisp after a resize.
  let resizeTimer = 0;
  on(window, 'resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (state.phase === 'ready') schedulePreview(false);
    }, PREVIEW_DEBOUNCE_MS * 2);
  });
}

function saveDataEnabled() {
  return Boolean(navigator.connection && navigator.connection.saveData);
}

function stepPage(delta) {
  const pages = (state.plan && state.plan.pages) || 1;
  const next = clamp(state.ui.page + delta, 0, pages - 1);
  if (next === state.ui.page) return;
  state.ui.page = next;
  renderChrome();
  schedulePreview(true);
}

/** Keeps the sheet's aspect ratio and margin guides in sync with the geometry. */
function renderSheetFrame() {
  const figure = el('sheet');
  const g = effectiveGeometry();
  if (figure && g && g.page_width_mm > 0 && g.page_height_mm > 0) {
    figure.style.aspectRatio = `${g.page_width_mm} / ${g.page_height_mm}`;
    figure.style.setProperty('--margin-top-pct', `${(state.margins.top / g.page_height_mm) * 100}%`);
    figure.style.setProperty('--margin-bottom-pct', `${(state.margins.bottom / g.page_height_mm) * 100}%`);
    figure.style.setProperty('--margin-left-pct', `${(state.margins.left / g.page_width_mm) * 100}%`);
    figure.style.setProperty('--margin-right-pct', `${(state.margins.right / g.page_width_mm) * 100}%`);
    figure.classList.toggle('show-margins', state.ui.showMargins);
  }
  const img = el('preview-image');
  if (img) img.classList.toggle('is-dimmed', state.ui.dimSheet);

  const showMargins = el('show-margins');
  if (showMargins) showMargins.checked = state.ui.showMargins;
  const dimSheet = el('dim-sheet');
  if (dimSheet) dimSheet.checked = state.ui.dimSheet;
}

// ---------------------------------------------------------------------------
// 10. Export
// ---------------------------------------------------------------------------

/** @returns {{allowed:boolean, reason:string}} whether a PDF can be produced right now. */
function exportReadiness() {
  if (state.phase !== 'ready') return { allowed: false, reason: 'Still loading the catalogue.' };
  if (!state.templateId) return { allowed: false, reason: 'Choose a label template first.' };
  const parsed = parseDocument(state.document);
  if (!parsed.ok) return { allowed: false, reason: 'Fix the JSON error in your records first.' };
  const plan = state.plan;
  if (plan && plan.geometry && plan.geometry.ok === false) {
    return { allowed: false, reason: 'This grid does not fit the page. Reduce the margins or the gaps.' };
  }
  if (templateFields().length > 0 && parsed.records.length === 0) {
    return { allowed: false, reason: 'Add at least one record before exporting.' };
  }
  const maxBytes = Number(state.limits.max_document_bytes) || Infinity;
  if (byteLength(state.document) > maxBytes) {
    return { allowed: false, reason: `Your records exceed the ${Math.round(maxBytes / 1024)} KB the server accepts.` };
  }
  const maxRecords = Number(state.limits.max_records) || Infinity;
  if (parsed.records.length > maxRecords) {
    return { allowed: false, reason: `That is ${parsed.records.length} records; the server accepts ${maxRecords}.` };
  }
  const maxPages = Number(state.limits.max_pages) || Infinity;
  if (plan && plan.pages > maxPages) {
    return { allowed: false, reason: `That would be ${plan.pages} pages; the server renders at most ${maxPages}.` };
  }
  return { allowed: true, reason: '' };
}

function renderExportState() {
  const button = el('download-pdf');
  const hint = el('export-hint');
  const readiness = exportReadiness();

  if (button) {
    setDisabled(button, !readiness.allowed || state.runtime.exporting);
    button.setAttribute('aria-busy', state.runtime.exporting ? 'true' : 'false');
    if (state.runtime.exporting) {
      setText(button, 'Building PDF...');
    } else {
      const pages = state.plan && state.plan.pages;
      setText(button, pages && pages > 1 ? `Download PDF - ${formatCount(pages, 'page', 'pages')}` : 'Download PDF');
    }
  }
  if (hint) {
    setText(hint, readiness.allowed ? '' : readiness.reason);
    setHidden(hint, readiness.allowed);
  }
}

async function exportPdf() {
  if (state.runtime.exporting) return;
  const readiness = exportReadiness();
  if (!readiness.allowed) {
    announce(readiness.reason);
    return;
  }

  state.runtime.exporting = true;
  setExportError(null);
  renderExportState();

  try {
    const result = await apiPdf(buildRenderRequest());
    const name = result.filename || `${state.filename || 'labels'}.pdf`;
    triggerDownload(result.blob, name);
    announce(`Downloaded ${name}.`);
  } catch (error) {
    if (!error || error.name !== 'AbortError') {
      setExportError(errorText(error));
      reportError(error, 'export');
    }
  } finally {
    state.runtime.exporting = false;
    renderExportState();
  }
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = make('a', 'sr-only', '', { href: url, download: filename });
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // Some browsers fetch the blob lazily, so the URL must outlive the click.
  window.setTimeout(() => URL.revokeObjectURL(url), OBJECT_URL_TTL_MS);
}

function setExportError(message) {
  const host = el('export-error');
  if (!host) return;
  clear(host);
  setHidden(host, !message);
  if (!message) return;
  host.setAttribute('role', 'alert');
  host.appendChild(make('p', 'alert__message', message));
}

// ---------------------------------------------------------------------------
// 11. Errors, toasts and persistence
// ---------------------------------------------------------------------------

/** @returns {string} a user-facing sentence for any thrown value. */
function errorText(error) {
  if (!error) return 'Something went wrong.';
  if (error instanceof ApiError) {
    const details = error.details.slice(0, 3).map(detailText).filter(Boolean);
    const suffix = details.length ? ` (${details.join('; ')})` : '';
    if (error.status === 429 || error.status === 503) {
      const wait = error.retryAfterS ? ` Try again in ${Math.ceil(error.retryAfterS)} s.` : '';
      return `${error.message}${wait}${suffix}`;
    }
    return `${error.message}${suffix}`;
  }
  return String(error.message || error);
}

function detailText(detail) {
  if (!detail) return '';
  const loc = Array.isArray(detail.loc) ? detail.loc.join('.') : detail.loc;
  const msg = detail.msg || detail.type || '';
  return loc ? `${loc}: ${msg}` : String(msg);
}

/** Routes an error to the surface that owns it, with a retry where retrying helps. */
function reportError(error, source) {
  const message = errorText(error);
  if (source === 'import') {
    setImportError(message);
    return;
  }
  if (source === 'export') return;

  if (error instanceof ApiError && error.status === 0 && state.runtime.failures >= MAX_CONSECUTIVE_FAILURES) {
    setStatus('attention');
    showToast('Offline - preview paused.', { actionLabel: 'Retry', onAction: () => { state.runtime.failures = 0; runPreview(); } });
    return;
  }
  const retryable = error instanceof ApiError ? error.retryable : false;
  showToast(message, retryable ? { actionLabel: 'Retry', onAction: () => runPreview() } : {});
}

/** @param {{actionLabel?:string, onAction?:Function, ttlMs?:number}} options */
function showToast(message, options) {
  const opts = options || {};
  let region = el('toast-region');
  if (!region) {
    region = make('div', 'toast-region', '', { id: 'toast-region', 'aria-live': 'polite' });
    document.body.appendChild(region);
  }
  const toast = make('div', 'toast', '', { role: 'alert', tabindex: '0' });
  toast.appendChild(make('span', 'toast__message', message));

  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  };
  if (opts.actionLabel && opts.onAction) {
    const action = make('button', 'btn btn--link', opts.actionLabel, { type: 'button' });
    on(action, 'click', () => {
      dismiss();
      opts.onAction();
    });
    toast.appendChild(action);
  }
  const close = make('button', 'btn btn--icon', 'Dismiss', { type: 'button', 'aria-label': 'Dismiss message' });
  on(close, 'click', dismiss);
  toast.appendChild(close);

  const ttl = opts.ttlMs || TOAST_MS;
  const start = () => { timer = window.setTimeout(dismiss, ttl); };
  // Pausing on hover or focus so a long message is readable.
  on(toast, 'mouseenter', () => window.clearTimeout(timer));
  on(toast, 'mouseleave', start);
  on(toast, 'focusin', () => window.clearTimeout(timer));
  on(toast, 'focusout', start);

  region.appendChild(toast);
  start();
}

// --- Storage --------------------------------------------------------------

/** Storage can throw (private mode, disabled cookies); it is never load-bearing. */
function storageGet(store, key) {
  try {
    return store.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(store, key, value) {
  try {
    store.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

const PERSISTED_KEYS = Object.freeze([
  'theme', 'unit', 'showMargins', 'dimSheet', 'recordsOpen', 'recordsHeight',
  'tab', 'templateId', 'layoutId', 'orientation', 'pageRotationDeg',
  'textRotationDeg', 'outlineSlots', 'filename', 'margins',
]);

function persistNow() {
  const payload = {
    theme: state.ui.theme,
    unit: state.ui.unit,
    showMargins: state.ui.showMargins,
    dimSheet: state.ui.dimSheet,
    recordsOpen: state.ui.recordsOpen,
    recordsHeight: state.ui.recordsHeight,
    tab: state.ui.tab,
    templateId: state.templateId,
    layoutId: state.layoutId,
    orientation: state.orientation,
    pageRotationDeg: state.pageRotationDeg,
    textRotationDeg: state.textRotationDeg,
    outlineSlots: state.outlineSlots,
    filename: state.filename,
    margins: state.margins,
  };
  for (const key of PERSISTED_KEYS) {
    storageSet(window.localStorage, LS + key, JSON.stringify(payload[key]));
  }
}

const persistSoon = debounce(persistNow, PERSIST_DEBOUNCE_MS, 'persistTimer');

function readPersisted(key, fallback) {
  const raw = storageGet(window.localStorage, LS + key);
  if (raw === null) return fallback;
  try {
    const value = JSON.parse(raw);
    return value === null || value === undefined ? fallback : value;
  } catch {
    return fallback;
  }
}

/** Records are session-scoped: surviving a refresh is useful, surviving a week is not. */
function persistDocument() {
  if (byteLength(state.document) > MAX_SESSION_DOCUMENT_BYTES) return;
  storageSet(window.sessionStorage, SS_DOCUMENT, state.document);
}

const persistDocumentSoon = debounce(persistDocument, PERSIST_DEBOUNCE_MS, 'documentTimer');

function restorePersisted() {
  state.ui.theme = THEMES.includes(readPersisted('theme', 'system')) ? readPersisted('theme', 'system') : 'system';
  state.ui.unit = readPersisted('unit', 'mm') === 'in' ? 'in' : 'mm';
  state.ui.showMargins = Boolean(readPersisted('showMargins', false));
  state.ui.dimSheet = Boolean(readPersisted('dimSheet', false));
  state.ui.recordsOpen = Boolean(readPersisted('recordsOpen', true));
  state.ui.recordsHeight = clamp(Number(readPersisted('recordsHeight', RECORDS_DEFAULT_H)), RECORDS_MIN_H, Math.max(RECORDS_MIN_H, Math.round(window.innerHeight * RECORDS_MAX_VIEWPORT_FRACTION)));
  state.ui.tab = readPersisted('tab', 'table') === 'json' ? 'json' : 'table';

  state.orientation = ORIENTATIONS.includes(readPersisted('orientation', 'portrait')) ? readPersisted('orientation', 'portrait') : 'portrait';
  const rotation = Number(readPersisted('pageRotationDeg', 0));
  state.pageRotationDeg = PAGE_ROTATIONS.includes(rotation) ? rotation : 0;
  const textRotation = readPersisted('textRotationDeg', null);
  state.textRotationDeg = Number.isFinite(Number(textRotation)) && textRotation !== null ? Number(textRotation) : null;
  state.outlineSlots = Boolean(readPersisted('outlineSlots', true));
  state.filename = sanitiseFilename(readPersisted('filename', 'labels'));

  const margins = readPersisted('margins', null);
  if (margins && MARGIN_SIDES.every((side) => Number.isFinite(Number(margins[side])))) {
    state.margins = {
      top: Number(margins.top), right: Number(margins.right),
      bottom: Number(margins.bottom), left: Number(margins.left),
    };
  }
}

/** The hash carries settings only. Sharing a link shares a configuration, never data. */
function writeHash() {
  const params = new URLSearchParams();
  if (state.templateId) params.set('t', state.templateId);
  if (state.layoutId) params.set('l', state.layoutId);
  if (state.orientation !== 'portrait') params.set('o', state.orientation);
  if (state.pageRotationDeg) params.set('r', String(state.pageRotationDeg));
  if (!state.outlineSlots) params.set('b', '0');
  if (state.ui.unit !== 'mm') params.set('u', state.ui.unit);
  const hash = params.toString();
  const url = `${window.location.pathname}${window.location.search}${hash ? `#${hash}` : ''}`;
  window.history.replaceState(null, '', url);
}

/** @returns {Object} settings parsed out of the URL hash, ignoring anything unknown. */
function readHash() {
  const raw = window.location.hash.replace(/^#/, '');
  if (!raw) return {};
  const params = new URLSearchParams(raw);
  const out = {};
  if (params.get('t')) out.templateId = params.get('t');
  if (params.get('l')) out.layoutId = params.get('l');
  if (ORIENTATIONS.includes(params.get('o'))) out.orientation = params.get('o');
  const rotation = Number(params.get('r'));
  if (PAGE_ROTATIONS.includes(rotation)) out.pageRotationDeg = rotation;
  if (params.get('b') === '0') out.outlineSlots = false;
  if (params.get('u') === 'in') out.unit = 'in';
  return out;
}

function resetEverything() {
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (key.startsWith(LS)) window.localStorage.removeItem(key);
    }
    window.sessionStorage.removeItem(SS_DOCUMENT);
  } catch {
    // Nothing to clean up if storage is unavailable.
  }
  window.location.hash = '';
  window.location.reload();
}

// --- Theme ----------------------------------------------------------------

function applyTheme() {
  const theme = state.ui.theme;
  document.documentElement.dataset.theme = theme;
  const button = el('theme-toggle');
  if (!button) return;
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  // The accessible name states the current value and what the press will do.
  button.setAttribute('aria-label', `Theme: ${theme}. Activate for ${next}.`);
  button.dataset.theme = theme;
}

function cycleTheme() {
  state.ui.theme = THEMES[(THEMES.indexOf(state.ui.theme) + 1) % THEMES.length];
  applyTheme();
  persistSoon();
  announce(`Theme: ${state.ui.theme}.`);
}

// ---------------------------------------------------------------------------
// 12. Chrome sync, keyboard, boot
// ---------------------------------------------------------------------------

/**
 * Unconditional, non-diffing sync of every derived label, count and aria state.
 * The page is small enough that rewriting it wholesale is free, and doing so
 * removes an entire class of staleness bugs.
 */
function renderChrome() {
  for (const input of all('input[name="orientation"]')) input.checked = input.value === state.orientation;
  for (const input of all('input[name="page-rotation"]')) input.checked = Number(input.value) === state.pageRotationDeg;

  const outline = el('outline-slots');
  if (outline) outline.checked = state.outlineSlots;

  const rotationToggle = el('text-rotation-enabled');
  const rotationInput = el('text-rotation');
  if (rotationToggle) rotationToggle.checked = state.textRotationDeg !== null;
  if (rotationInput) {
    setDisabled(rotationInput, state.textRotationDeg === null);
    const field = rotationInput.closest('.rail-field');
    if (field) setHidden(field, state.textRotationDeg === null);
    if (document.activeElement !== rotationInput) rotationInput.value = state.textRotationDeg === null ? '0' : String(state.textRotationDeg);
  }

  const filename = el('filename');
  if (filename && document.activeElement !== filename) filename.value = state.filename;

  const layout = el('layout-select');
  if (layout) layout.value = state.layoutId || '';

  renderMarginInputs();
  renderTemplateMeta();
  renderSheetFrame();
  renderStats();
  renderRecordsChrome();
  renderExportState();
  setText(el('version-chip'), state.version ? `v${state.version}` : '');
}

function renderStats() {
  const plan = state.plan;
  const info = currentTemplate();
  setText(el('stat-labels'), plan ? String(plan.labels) : '0');
  setText(el('stat-pages'), plan ? String(plan.pages) : '0');
  setText(el('stat-per-sheet'), String((plan && plan.labels_per_page) || (info && info.labels_per_page) || 0));
  setText(el('sheet-caption'), describeSheet());

  const pages = (plan && plan.pages) || 1;
  setHidden(el('page-stepper'), pages <= 1);
  setText(el('page-indicator'), `${state.ui.page + 1} / ${pages}`);
  setDisabled(el('page-prev'), state.ui.page <= 0);
  setDisabled(el('page-next'), state.ui.page >= pages - 1);

  setText(el('context-summary'), info ? `${info.name} - ${state.orientation === 'landscape' ? 'Landscape' : 'Portrait'}` : '');
}

function wireGlobalKeys() {
  on(document, 'keydown', (event) => {
    const mod = event.metaKey || event.ctrlKey;
    // Every binding uses a modifier, so typing in an editor is never ambiguous.
    if (mod && event.key === 'Enter') {
      event.preventDefault();
      exportPdf();
      return;
    }
    if (mod && !event.shiftKey && event.key.toLowerCase() === 's') {
      event.preventDefault();
      exportPdf();
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'f') {
      if (state.ui.tab !== 'json') return;
      event.preventDefault();
      formatJson();
      return;
    }
    if (event.key === 'Escape') {
      const dialog = document.querySelector('dialog[open]');
      if (dialog && typeof dialog.close === 'function') {
        dialog.close();
        return;
      }
      if (state.ui.recordsOpen) {
        state.ui.recordsOpen = false;
        renderRecordsChrome();
        persistSoon();
      }
    }
  });
}

function wireStaticControls() {
  on(el('theme-toggle'), 'click', cycleTheme);
  on(el('download-pdf'), 'click', exportPdf);
  on(el('reset-everything'), 'click', () => {
    const dialog = el('reset-dialog');
    if (dialog && typeof dialog.showModal === 'function') {
      dialog.showModal();
      return;
    }
    resetEverything();
  });
  on(el('reset-confirm'), 'click', resetEverything);

  const openGenerator = el('open-generator');
  on(openGenerator, 'click', (event) => {
    event.preventDefault();
    const workspace = el('workspace');
    if (!workspace) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    workspace.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    const first = document.querySelector('input[name="label-template"]');
    if (first) first.focus({ preventScroll: true });
  });

  // The masthead's bottom rule appears only once the page has scrolled.
  const masthead = el('masthead');
  if (masthead) {
    const onScroll = () => masthead.classList.toggle('is-scrolled', window.scrollY > 8);
    on(window, 'scroll', onScroll, { passive: true });
    onScroll();
  }
}

function showBootError(error) {
  state.phase = 'failed';
  state.bootError = error;
  const host = el('boot-error');
  if (!host) {
    showToast(errorText(error), { actionLabel: 'Retry', onAction: boot });
    return;
  }
  clear(host);
  setHidden(host, false);
  host.setAttribute('role', 'alert');
  host.appendChild(make('p', 'alert__title', 'Could not load the template catalogue'));
  host.appendChild(make('p', 'alert__message', errorText(error)));
  if (error instanceof ApiError && error.requestId) {
    host.appendChild(make('p', 'alert__meta', `Request ${error.requestId}`));
  }
  const retry = make('button', 'btn btn--primary', 'Retry', { type: 'button' });
  on(retry, 'click', () => {
    setHidden(host, true);
    boot();
  });
  host.appendChild(retry);
  setStatus('attention');
  renderExportState();
}

/** Applies a bootstrap payload defensively: any field may be absent on an older server. */
function ingestBootstrap(payload) {
  const templates = Array.isArray(payload.templates) ? payload.templates : [];
  state.templates = templates.filter((info) => info && info.kind !== 'text-layout');
  state.layouts = Array.isArray(payload.layouts) ? payload.layouts : templates.filter((info) => info && info.kind === 'text-layout');
  state.broken = Array.isArray(payload.broken) ? payload.broken : [];
  state.features = payload.features || {};
  state.version = (payload.version && payload.version.version) || '';
  if (payload.limits && typeof payload.limits === 'object') {
    state.limits = { ...state.limits, ...payload.limits };
  }
}

/** Chooses the starting template: hash, then last session, then the first on offer. */
function chooseInitialTemplate(hash) {
  const ids = new Set(state.templates.map((info) => info.id));
  const persisted = readPersisted('templateId', null);
  if (hash.templateId && ids.has(hash.templateId)) return hash.templateId;
  if (persisted && ids.has(persisted)) return persisted;
  return state.templates.length ? state.templates[0].id : null;
}

function chooseInitialLayout(hash) {
  const ids = new Set(state.layouts.map((info) => info.id));
  const persisted = readPersisted('layoutId', null);
  if (hash.layoutId && ids.has(hash.layoutId)) return hash.layoutId;
  if (persisted && ids.has(persisted)) return persisted;
  return null;
}

function setSkeleton(active) {
  document.documentElement.classList.toggle('is-booting', Boolean(active));
  for (const node of all('[data-skeleton]')) setHidden(node, !active);
}

async function boot() {
  state.phase = 'booting';
  setSkeleton(true);
  setStatus('rendering');
  renderTemplateCards();

  let payload;
  try {
    payload = await apiBootstrap();
  } catch (error) {
    setSkeleton(false);
    showBootError(error);
    return;
  }

  ingestBootstrap(payload);
  const hash = readHash();
  if (hash.orientation) state.orientation = hash.orientation;
  if (hash.pageRotationDeg !== undefined) state.pageRotationDeg = hash.pageRotationDeg;
  if (hash.outlineSlots === false) state.outlineSlots = false;
  if (hash.unit) state.ui.unit = hash.unit;

  state.templateId = chooseInitialTemplate(hash);
  state.layoutId = chooseInitialLayout(hash);

  const info = currentTemplate();
  if (info) {
    const persistedMargins = readPersisted('margins', null);
    adoptTemplateMargins(info);
    // Only honour stored margins when they belong to the template we restored.
    if (persistedMargins && readPersisted('templateId', null) === state.templateId) {
      state.margins = {
        top: Number(persistedMargins.top) || 0,
        right: Number(persistedMargins.right) || 0,
        bottom: Number(persistedMargins.bottom) || 0,
        left: Number(persistedMargins.left) || 0,
      };
    }
  }

  // Typed work outlives a refresh. A fresh tab starts from the template's own
  // sample records when it ships any, so the first thing a visitor sees is a
  // real sheet rather than an empty page with an empty preview.
  const restored = storageGet(window.sessionStorage, SS_DOCUMENT);
  state.document = restored !== null ? restored : startingDocument();

  state.phase = 'ready';
  setSkeleton(false);
  state.runtime.autoRender = !saveDataEnabled();
  setHidden(el('render-now'), state.runtime.autoRender);

  renderTemplateCards();
  renderLayoutOptions();
  renderBrokenTemplates();
  syncJsonEditor();
  renderTable();
  renderChrome();
  writeHash();

  if (state.runtime.autoRender) runPreview();
  else setStatus('stale');
}

function init() {
  restorePersisted();
  applyTheme();
  wireStaticControls();
  wireSettingsRail();
  wireRecordsEditor();
  wirePreviewControls();
  wireGlobalKeys();
  boot();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
