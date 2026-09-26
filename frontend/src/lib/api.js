export const API = (p) => `${import.meta.env.VITE_API_URL || ''}${p}`;
/** Unwrap the {data, error, meta} envelope; throw coded Error on error envelope. */
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
  const r = await fetch(API(path), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) {
    let code = `HTTP ${r.status}`;
    try { const e = await r.json(); if (e?.error?.code) code = e.error.code; } catch {}
    throw new Error(code);
  }
  return r.json();
}
/** POST expecting a non-JSON body (CSV download). Returns {status, text, filename}. */
export async function postText(path, body) {
  const r = await fetch(API(path), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const disp = r.headers.get('Content-Disposition') || '';
  const m = /filename=([^;]+)/.exec(disp);
  return { text: await r.text(), filename: m ? m[1].trim() : 'dataset.csv' };
}
export async function getJSON(path) { const r = await fetch(API(path)); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
