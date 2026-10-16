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

// `import.meta.env` is injected by Vite and is `undefined` under plain Node,
// so this module could not be imported by a test runner at all. Optional
// chaining keeps the default working outside a bundler instead of throwing on
// module load.
const BASE = ((import.meta.env && import.meta.env.VITE_API_BASE) || '/api').replace(/\/$/, '');
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

/**
 * Open an authenticated SSE stream.
 *
 * `EventSource` cannot send headers, and this API gates every route except
 * `/api/health` on `X-API-Key`. Using EventSource therefore meant the stream
 * 401'd on every keyed install while `run-context` — which polls with the key —
 * reported the run as live, so the header showed progress and the rail showed
 * nothing, forever.
 *
 * The obvious workaround, `?api_key=` in the query string, puts a credential in
 * a URL: it lands in access logs, in `Referer`, and in browser history. So this
 * reads the response body as a stream instead, which carries headers normally.
 *
 * Server frames look like:
 *
 *     event: stage.started
 *     data: {...}
 *
 * and are parsed here into `{ event, data }` records. `onFrame` is called for
 * each; a parse error or a frame the server did not label is passed through as
 * `event: 'message'` rather than dropped, matching EventSource's behaviour.
 *
 * Returns an object with `close()`. The reader stops when `signal` aborts, when
 * the stream ends, or when `close()` is called.
 */
export async function openStream(path, { signal, onFrame, onOpen, onError } = {}) {
  const headers = { Accept: 'text/event-stream' };
  const key = apiKey();
  if (key) headers['X-API-Key'] = key;

  const response = await fetch(`${BASE}${path}`, {
    headers,
    signal,
    cache: 'no-store',
  });

  if (!response.ok) {
    // Surface the server's reason rather than a bare status. A 401 here almost
    // always means the key is missing or wrong, which is worth saying outright.
    let detail = `stream refused (HTTP ${response.status})`;
    try {
      const body = await response.json();
      detail = body?.error?.message || body?.detail || detail;
    } catch {
      /* non-JSON body; the status is all we have */
    }
    throw new Error(detail);
  }

  onOpen?.();
  return readEventStream(response.body, { onFrame, signal });
}

/** Parse an SSE body into frames. Exported so the parser can be tested alone. */
export async function readEventStream(body, { onFrame, signal } = {}) {
  if (!body) return;
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const dispatch = (block) => {
    let event = 'message';
    const dataLines = [];
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue; // comment / keep-alive
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length && event === 'message') return;
    const raw = dataLines.join('\n');
    let parsed = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      /* not JSON; pass the text through */
    }
    onFrame?.({ event, data: parsed });
  };

  try {
    for (;;) {
      if (signal?.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line. Normalise CRLF first, since a
      // proxy may rewrite line endings and a lone \r would otherwise never match.
      buffer = buffer.replace(/\r\n/g, '\n');
      let index;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        if (block.trim()) dispatch(block);
      }
    }
    if (buffer.trim()) dispatch(buffer);
  } catch (err) {
    if (!signal?.aborted) throw err;
  } finally {
    try {
      reader.cancel();
    } catch {
      /* already closed */
    }
  }
}

export const api = {
  /* --- system ---------------------------------------------------------- */
  health: (o) => request('/health', { ...o, timeout: o?.timeout ?? 10_000 }),

  /* --- planning / execution ------------------------------------------- */
  // Plan compilation calls the model (and cascades providers when the primary
  // is cold or down), so the 45s default turns a slow compile into a UI
  // timeout while the server is still working — the exact shape seen live.
  compile: (body, o) => request('/workflows/compile', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
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
  // Named `sourceHosts`, not `sources`: `sources` below is the *per-dataset*
  // route. Two keys of the same name in one object literal means the second
  // silently wins, which would repoint the dataset Sources tab at the global
  // aggregate and have it 404 on every dataset id.
  sourceHosts: (params, o) => request(`/sources${qs(params)}`, o),
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
  // The dataset agent. The model picks read tools, the server runs them, and
  // the answer is composed from the results — no model-authored SQL, and
  // nothing here writes. Two model calls at most, same budget as a query.
  askAgent: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/agent`, { ...o, method: 'POST', body, timeout: o?.timeout ?? 300_000 }),
  // Delete one dataset. The server requires an exact `confirm` equal to the
  // id and refuses anything else, so a stray call deletes nothing — but the
  // UI still asks twice, because the one destructive action in the product
  // should feel destructive before it is one.
  deleteDataset: (id, o) =>
    request(`/datasets/${encodeURIComponent(id)}`, { ...o, method: 'DELETE', body: { confirm: id } }),
  refinePlan: (body, o) =>
    request('/workflows/refine', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  backfillProposal: (id, params, o) =>
    request(`/datasets/${encodeURIComponent(id)}/backfill${qs(params)}`, o),
  backfill: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/backfill`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),

  // --- two-phase gap filling ----------------------------------------------
  // `gaps` is a read. `fillGap` is a real search-and-fetch over the network, so
  // it gets the long timeout — and the UI always calls it with `apply: false`
  // first, because the dry run performs the search and the extraction and only
  // withholds the write, which is the only way to see the cost before paying it.
  gaps: (id, o) => request(`/datasets/${encodeURIComponent(id)}/gaps`, o),
  fillGap: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/gaps/fill`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),

  // --- re-judging the cells that were extracted right and never checked -----
  // Most of these are settled by a substring check and cost nothing; a minority
  // spend a judge call. Still a network round trip per cell for the ones that
  // do, so it gets the long timeout and the same dry-run-then-apply shape.
  rejudge: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/rejudge`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),

  // --- learned yield, refresh, columns, dashboard ---------------------------
  // `yield` is a read of the quotes every verified cell already carries, so it
  // is cheap and never a source of its own claims. Refresh re-fetches over the
  // network, so it gets the same long timeout as a backfill.
  // The per-field shape and the place distribution. Read from the server so
  // the numbers are the same ones the Coverage view shows, rather than a
  // second opinion computed from whatever the records table paged in.
  profile: (id, params, o) =>
    request(`/datasets/${encodeURIComponent(id)}/profile${qs(params)}`, o),
  yieldMap: (id, o) => request(`/datasets/${encodeURIComponent(id)}/yield`, o),
  refreshProposal: (id, params, o) =>
    request(`/datasets/${encodeURIComponent(id)}/refresh${qs(params)}`, o),
  refresh: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/refresh`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 900_000,
    }),
  addColumn: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/columns`, { ...o, method: 'POST', body }),
  dashboard: (params, o) =>
    // Cold, this aggregate reads a bounded sample of every dataset's records
    // for the host ranking (the coverage itself comes from one query). That is
    // ~28s on the current library against a 300s server cache — inside 45s, but
    // close enough that a bigger library would show a false timeout error.
    // The call gets its own budget rather than raising the default for all.
    request(`/dashboard${qs(params)}`, { ...o, timeout: o?.timeout ?? 120_000 }),
  proposeChange: (id, body, o) =>
    request(`/datasets/${encodeURIComponent(id)}/propose`, {
      ...o, method: 'POST', body, timeout: o?.timeout ?? 60_000,
    }),
  proposeSelectors: (body, o) =>
    request('/selectors/propose', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  saveSelectors: (body, o) =>
    request('/selectors/save', { ...o, method: 'POST', body, timeout: o?.timeout ?? 30_000 }),

  /**
   * Export a dataset.
   *
   * Every format comes back in the same envelope: `{data: {format, content}, …}`
   * where `content` is the rendered text. This used to ask for a Blob for
   * `csv` and `md` on the belief that they were raw attachments — they are not.
   * The handler returns the envelope for every format, so `res.blob()` produced
   * the JSON bytes `{"data":{"format":"csv","content":"…"}}` and the user
   * downloaded it as `dataset-xxxxxxxx.csv`: an envelope containing a CSV,
   * opened by a spreadsheet as a single broken cell.
   *
   * The blob is therefore built here, from `content`, and the filename comes from
   * the format rather than being assumed by the caller.
   */
  async exportDataset(id, { format = 'json', fields } = {}, o = {}) {
    const body = { format };
    if (fields?.length) body.fields = fields;
    const res = await request(`/datasets/${encodeURIComponent(id)}/export`, {
      ...o,
      method: 'POST',
      body,
    });
    const actual = res?.format || format;
    const content = res?.content ?? '';
    return {
      format: actual,
      content,
      rows: res?.rows,
      bytes: res?.bytes,
      exportId: res?.export_id,
      blob: new Blob([content], {
        type: actual === 'csv'
          ? 'text/csv;charset=utf-8'
          : actual === 'md'
            ? 'text/markdown;charset=utf-8'
            : 'application/json;charset=utf-8',
      }),
      filename: exportFilename(id, actual),
    };
  },

  /* --- intel ----------------------------------------------------------- */
  map: (body, o) => request('/map', { ...o, method: 'POST', body, timeout: o?.timeout ?? 180_000 }),
  freeModels: (o) => request('/models/free', { ...o, timeout: o?.timeout ?? 30_000 }),
  ask: (body, o) => request('/intel/ask', { ...o, method: 'POST', body, timeout: o?.timeout ?? 300_000 }),
};

/** Filename for an export, derived from the format the server actually used. */
export function exportFilename(datasetId, format) {
  const ext = { csv: 'csv', json: 'json', jsonl: 'jsonl', md: 'md' }[format] || 'txt';
  return `dataset-${String(datasetId || 'export').slice(0, 8)}.${ext}`;
}

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
