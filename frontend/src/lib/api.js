/**
 * API client.
 *
 * Every backend response is the envelope `{data, error, meta}`. This module is
 * the one place that unwraps it, so components only ever see `data` or a
 * thrown `ApiError` with a message worth showing a user.
 *
 * Also handles: request timeouts, caller aborts, the optional X-API-Key
 * header, and binary (export) responses.
 */

const BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');
const DEFAULT_TIMEOUT = 45_000;

export class ApiError extends Error {
  constructor(message, { status, detail, path, payload } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.path = path;
    this.payload = payload;
  }
}

/** Validated `extra.detail` objects are surfaced, not swallowed. */
function describeError(error, payload) {
  if (!error) return null;
  if (typeof error === 'string') return error;

  if (error.code) {
    return error.message ? `${error.code}: ${error.message}` : error.code;
  }
  if (Array.isArray(error.details) && error.details.length) {
    const bits = error.details.slice(0, 3).map((d) => {
      const loc = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : null;
      const msg = d.msg || d.message || 'invalid';
      return loc ? `${loc} — ${msg}` : msg;
    });
    const extra = error.details.length > 3 ? ` (+${error.details.length - 3} more)` : '';
    return `${error.message || 'Validation failed'}: ${bits.join('; ')}${extra}`;
  }
  if (error.message) return error.message;
  if (payload?.detail) {
    return typeof payload.detail === 'string'
      ? payload.detail
      : JSON.stringify(payload.detail);
  }
  return 'Request failed';
}

export function apiKey() {
  try {
    return window.localStorage.getItem('dg_api_key') || '';
  } catch {
    return '';
  }
}

export function setApiKey(value) {
  try {
    if (value) window.localStorage.setItem('dg_api_key', value);
    else window.localStorage.removeItem('dg_api_key');
  } catch {
    /* private mode — key just won't persist */
  }
}

/** Compose an external signal with a timeout signal. */
function withTimeout(external, ms) {
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, ms);
  const onAbort = () => ctrl.abort();
  if (external) {
    if (external.aborted) ctrl.abort();
    else external.addEventListener('abort', onAbort, { once: true });
  }
  return {
    signal: ctrl.signal,
    get timedOut() {
      return timedOut;
    },
    dispose() {
      clearTimeout(timer);
      external?.removeEventListener('abort', onAbort);
    },
  };
}

async function request(path, { method = 'GET', body, signal, timeout, raw } = {}) {
  const guard = withTimeout(signal, timeout ?? DEFAULT_TIMEOUT);
  const headers = { Accept: 'application/json' };
  const key = apiKey();
  if (key) headers['X-API-Key'] = key;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: guard.signal,
    });
  } catch (err) {
    if (guard.timedOut) {
      throw new ApiError(`Request timed out after ${Math.round((timeout ?? DEFAULT_TIMEOUT) / 1000)}s`, { path });
    }
    if (err?.name === 'AbortError') {
      throw new ApiError('Request cancelled', { path });
    }
    throw new ApiError(
      'Cannot reach the API. Is the backend running on port 8000?',
      { path, detail: err?.message }
    );
  } finally {
    guard.dispose();
  }

  if (raw) {
    if (!res.ok) {
      let payload = null;
      try {
        payload = await res.json();
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(describeError(payload?.error, payload) || res.statusText, {
        status: res.status,
        path,
        payload,
      });
    }
    return res.blob();
  }

  // 204 / empty
  if (res.status === 204) return null;

  let payload = null;
  try {
    payload = await res.json();
  } catch {
    if (!res.ok) throw new ApiError(res.statusText || `HTTP ${res.status}`, { status: res.status, path });
    return null;
  }

  if (!res.ok || payload?.error) {
    throw new ApiError(describeError(payload?.error, payload) || `HTTP ${res.status}`, {
      status: res.status,
      detail: payload?.error?.detail,
      path,
      payload,
    });
  }

  return payload?.data !== undefined ? payload.data : payload;
}

const qs = (params) => {
  const s = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') s.set(k, String(v));
  });
  const out = s.toString();
  return out ? `?${out}` : '';
};

export const api = {
  /* --- system ---------------------------------------------------------- */
  health: (o) => request('/health', { ...o, timeout: o?.timeout ?? 10_000 }),

  /* --- planning / execution ------------------------------------------- */
  compile: (body, o) => request('/workflows/compile', { ...o, method: 'POST', body }),
  createRun: (body, o) => request('/runs', { ...o, method: 'POST', body }),
  run: (id, o) => request(`/runs/${encodeURIComponent(id)}`, o),
  cancelRun: (id, o) => request(`/runs/${encodeURIComponent(id)}/cancel`, { ...o, method: 'POST' }),
  jobs: (runId, o) => request(`/jobs/${encodeURIComponent(runId)}`, o),

  /* --- history --------------------------------------------------------- */
  // takes no query parameters; the signature exists for symmetry
  history: (o) => request('/history', o),

  /* --- stored evidence -------------------------------------------------- */
  // A quote carries page_id + start/end. These are what make that arithmetic
  // mean something: the offsets address `page.markdown`, which is the same
  // string the evidence store and the extractor both build, so a quote can be
  // shown sitting inside the page it was taken from instead of asserted.
  page: (pageId, o) => request(`/pages/${encodeURIComponent(pageId)}`, { ...o, timeout: o?.timeout ?? 30_000 }),
  runPages: (runId, params, o) =>
    request(`/runs/${encodeURIComponent(runId)}/pages${qs(params)}`, o),

  /* --- datasets -------------------------------------------------------- */
  datasets: (o) => request('/datasets', o),
  dataset: (id, o) => request(`/datasets/${encodeURIComponent(id)}`, o),
  records: (id, params, o) => request(`/datasets/${encodeURIComponent(id)}/records${qs(params)}`, o),
  sources: (id, o) => request(`/datasets/${encodeURIComponent(id)}/sources`, o),
  coverage: (id, o) => request(`/datasets/${encodeURIComponent(id)}/coverage`, o),
  conflicts: (id, o) => request(`/datasets/${encodeURIComponent(id)}/conflicts`, o),
  resolveConflict: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/conflicts/resolve`, { ...o, method: 'POST', body }),
  selectors: (o) => request('/selectors', o),
  queryDataset: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/query`, { ...o, method: 'POST', body, timeout: o?.timeout ?? 300_000 }),
  refinePlan: (body, o) =>
    request('/workflows/refine', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  backfillProposal: (id, params, o) =>
    request(`/datasets/${encodeURIComponent(id)}/backfill${qs(params)}`, o),
  backfill: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/backfill`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),

  // --- learned yield, refresh, columns, dashboard ---------------------------
  // `yield` is a read of the quotes every verified cell already carries, so it
  // is cheap and never a source of its own claims. Refresh re-fetches over the
  // network, so it gets the same long timeout as a backfill.
  yieldMap: (id, o) => request(`/datasets/${encodeURIComponent(id)}/yield`, o),
  refreshProposal: (id, params, o) =>
    request(`/datasets/${encodeURIComponent(id)}/refresh${qs(params)}`, o),
  refresh: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/refresh`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),
  addColumn: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/columns`, { ...o, method: 'POST', body }),
  dashboard: (params, o) => request(`/dashboard${qs(params)}`, o),
  proposeChange: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/propose`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 60_000,
    }),
  proposeSelectors: (body, o) =>
    request('/selectors/propose', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  saveSelectors: (body, o) =>
    request('/selectors/save', { ...o, method: 'POST', body, timeout: o?.timeout ?? 30_000 }),

  /**
   * Export. Deliberately asymmetric: `csv` and `md` come back as a raw
   * attachment that bypasses the `{data,error,meta}` envelope entirely, while
   * `json` is a normal enveloped response. Asking for a Blob on all three
   * would hand the caller `{"data":…}` bytes for JSON, so the branch is here.
   */
  async exportDataset(id, { format = 'json', fields } = {}, o = {}) {
    const body = { format };
    if (fields?.length) body.fields = fields;
    if (format === 'json') {
      return request(`/datasets/${encodeURIComponent(id)}/export`, {
        ...o,
        method: 'POST',
        body,
      });
    }
    return {
      blob: await request(`/datasets/${encodeURIComponent(id)}/export`, {
        ...o,
        method: 'POST',
        body,
        raw: true,
      }),
      format,
    };
  },

  /* --- intel ----------------------------------------------------------- */
  map: (body, o) => request('/map', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  freeModels: (o) => request('/models/free', { ...o, timeout: o?.timeout ?? 30_000 }),
  ask: (body, o) => request('/intel/ask', { ...o, method: 'POST', body, timeout: o?.timeout ?? 300_000 }),
};

/** Trigger a browser download from a blob without leaking the object URL. */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'export';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
