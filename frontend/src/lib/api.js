export const API = (p) => `${import.meta.env.VITE_API_URL || ''}${p}`;

/**
 * Pull a human-readable message out of whatever shape came back.
 *
 * The backend answers with `{data, error, meta}`, but FastAPI's own validation
 * errors used to arrive as `{"detail": [...]}` - a different shape, and the
 * old handler only looked for `error.code`, so a 422 surfaced as the bare text
 * "E_VALIDATION" with the actual reason discarded. The backend now returns the
 * envelope for those too, but both shapes are handled so a mismatch can never
 * leave the user staring at a blank error again.
 */
function messageOf(payload, status) {
  if (!payload) return `HTTP ${status}`;
  if (payload?.error?.message) return payload.error.message;
  if (typeof payload?.error === 'string') return payload.error;
  if (Array.isArray(payload?.detail)) {
    const first = payload.detail[0];
    if (first) {
      const where = Array.isArray(first.loc)
        ? first.loc.filter((x) => x !== 'body' && x !== 'query').join('.')
        : '';
      const msg = first.msg || 'invalid request';
      return where ? `${where}: ${msg}` : msg;
    }
  }
  if (payload?.detail) return String(payload.detail);
  if (payload?.message) return String(payload.message);
  return `HTTP ${status}`;
}

function codeOf(payload) {
  return payload?.error?.code || undefined;
}

function fail(status, payload) {
  const e = new Error(messageOf(payload, status));
  e.status = status;
  e.code = codeOf(payload);
  e.details = payload?.error?.details;
  throw e;
}

/** Unwrap the {data, error, meta} envelope; throw a coded Error on error. */
export function unwrap(envelope) {
  if (envelope && envelope.error) {
    const e = new Error(envelope.error.message || 'Request failed');
    e.code = envelope.error.code;
    e.details = envelope.error.details;
    throw e;
  }
  return (envelope || {}).data;
}

export async function postJSON(path, body) {
  const r = await fetch(API(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await r.json(); } catch { /* empty or non-JSON body */ }
  if (!r.ok) fail(r.status, payload);
  return payload;
}

/** POST expecting a non-JSON body (CSV download). Returns {status, text, filename}. */
export async function postText(path, body) {
  const r = await fetch(API(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let payload = null;
    try { payload = await r.json(); } catch { /* ignore */ }
    fail(r.status, payload);
  }
  const disp = r.headers.get('Content-Disposition') || '';
  const m = /filename=([^;]+)/.exec(disp);
  return { text: await r.text(), filename: m ? m[1].trim() : 'dataset.csv' };
}

export async function getJSON(path) {
  const r = await fetch(API(path));
  let payload = null;
  try { payload = await r.json(); } catch { /* ignore */ }
  if (!r.ok) fail(r.status, payload);
  return payload;
}
