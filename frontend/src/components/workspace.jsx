import { useEffect, useMemo, useState } from 'react';
import { useRunStream } from '../hooks/useRunStream.js';
import { getJSON, postJSON, postText, unwrap } from '../lib/api.js';

/* Fieldwork-style collection workspace: ask → plan → live run → table → proof.
 * Server state only (fetch + SSE); no global store. Every control is wired. */

const STEPS = [
  { key: 'DISCOVER', sub: 'Find relevant sources and targets' },
  { key: 'FETCH', sub: 'Collect data from web and APIs' },
  { key: 'EXTRACT', sub: 'Parse and structure key fields' },
  { key: 'VERIFY', sub: 'Check against sources' },
  { key: 'MERGE', sub: 'Remove duplicates and finalize' },
];

const AVATAR_BG = ['#1F2937', '#7C3A2D', '#2D5A3D', '#4A3B6B', '#8A6D1B', '#0E7490'];
function avatarColor(name) {
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_BG[h % AVATAR_BG.length];
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString('en-GB', { hour12: false });
  } catch {
    return '';
  }
}

function cellOf(record, name) {
  const f = record?.fields || {};
  return f[name];
}
function cellValue(cell) {
  if (cell == null) return '';
  if (typeof cell === 'object') return cell.value ?? '';
  return cell;
}
function recordStatus(record, cols) {
  const ss = cols.map((c) => cellOf(record, c)).filter((c) => c && typeof c === 'object')
    .map((c) => c.verification_status);
  if (ss.includes('needs_review')) return 'needs_review';
  if (ss.length && ss.every((s) => s === 'verified')) return 'verified';
  return 'unverified';
}

function StatusPill({ status }) {
  if (status === 'verified')
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-emerald-800">
        <span className="inline-block h-2 w-2 rounded-full bg-emerald-600" aria-hidden />Verified
      </span>
    );
  if (status === 'needs_review')
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-amber-800">
        <span className="inline-block h-2 w-2 rounded-full bg-amber-500" aria-hidden />Pending
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-stone-500">
      <span className="inline-block h-2 w-2 rounded-full bg-stone-400" aria-hidden />Unverified
    </span>
  );
}

function Stepper({ events, started }) {
  const types = new Set(events.map((e) => e.type));
  let reached = 0;
  if (started) reached = 1;
  if (types.has('source.discovered')) reached = Math.max(reached, 1);
  if (types.has('source.fetched')) reached = Math.max(reached, 2);
  if (types.has('record.extracted')) reached = Math.max(reached, 3);
  if (types.has('record.verified') || types.has('record.rejected')) reached = Math.max(reached, 4);
  if (types.has('run.completed')) reached = 5;
  return (
    <ol className="flex items-start gap-0" aria-label="Collection progress">
      {STEPS.map((s, i) => {
        const n = i + 1;
        const done = n < reached || (n === 5 && reached === 5);
        const current = n === reached && reached < 5;
        return (
          <li key={s.key} className="flex flex-1 items-start last:flex-none">
            <div className="flex flex-col items-center text-center">
              <span
                aria-current={current ? 'step' : undefined}
                className={
                  'flex h-7 w-7 items-center justify-center rounded-full border text-xs font-semibold ' +
                  (done || current
                    ? 'border-accent bg-accent text-white'
                    : 'border-rule bg-warm text-muted')
                }
              >
                {n}
              </span>
              <span className="mt-1.5 text-[11px] font-bold tracking-wider">{s.key}</span>
              <span className="mt-0.5 hidden max-w-[130px] text-[11px] leading-tight text-muted xl:block">
                {s.sub}
              </span>
            </div>
            {n < 5 && <span className="mx-2 mt-3.5 h-px flex-1 bg-rule" aria-hidden />}
          </li>
        );
      })}
    </ol>
  );
}

function ProofInspector({ record, field, cols, onClose }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  if (!record || !field) return null;
  const cell = cellOf(record, field);
  const value = cellValue(cell);
  const src = (cell && typeof cell === 'object' && cell.source) || {};
  const status = (cell && typeof cell === 'object' && cell.verification_status) || 'unverified';
  const title = cellValue(cellOf(record, cols[0]));
  let domain = '';
  try {
    domain = new URL(src.url).hostname.replace(/^www\./, '');
  } catch {
    domain = '';
  }
  return (
    <aside
      aria-label="Proof inspector"
      className="flex w-full flex-col gap-4 rounded-lg border border-rule bg-warm p-5"
    >
      <div className="flex items-start justify-between">
        <h3 className="font-display text-lg">Proof inspector</h3>
        <button onClick={onClose} aria-label="Close proof inspector" className="rounded p-1 text-muted hover:text-ink">
          ✕
        </button>
      </div>
      <div>
        <p className="text-sm text-muted">
          {field} · {title}
        </p>
        <div className="mt-1 flex items-center gap-3">
          <span className="font-display text-4xl">${String(value).slice(0, 24)}</span>
          {status === 'verified' && (
            <span className="rounded-full border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">
              ✓ VERIFIED
            </span>
          )}
        </div>
      </div>
      {src.url ? (
        <div className="flex items-start gap-3 border-t border-rule pt-4">
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-sm font-bold text-white"
            style={{ background: avatarColor(domain) }}
            aria-hidden
          >
            {domain.slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0 text-sm">
            <p className="font-semibold">{domain || 'Source'}</p>
            {src.retrieved_at && <p className="text-xs text-muted">{fmtTime(src.retrieved_at)}</p>}
            <a className="break-all text-xs text-accent underline" href={src.url} target="_blank" rel="noreferrer">
              {src.url}
            </a>
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted">No source attached to this field yet.</p>
      )}
      {src.quote && (
        <div>
          <h4 className="text-sm font-semibold">Evidence</h4>
          <blockquote className="mt-1 rounded bg-[#EFE9DB] p-3 text-sm leading-relaxed">
            “{src.quote}”
          </blockquote>
          <a
            className="mt-3 inline-block rounded-md border border-rule px-3 py-1.5 text-sm hover:bg-white"
            href={src.url}
            target="_blank"
            rel="noreferrer"
          >
            Open source ↗
          </a>
        </div>
      )}
      <dl className="grid grid-cols-2 gap-y-1 border-t border-rule pt-3 text-xs">
        <dt className="text-muted">Field</dt>
        <dd className="text-right font-medium">{field}</dd>
        <dt className="text-muted">Value</dt>
        <dd className="text-right font-medium">{String(value).slice(0, 60)}</dd>
        <dt className="text-muted">Status</dt>
        <dd className="text-right font-medium capitalize">{status.replace('_', ' ')}</dd>
        <dt className="text-muted">Retrieved</dt>
        <dd className="text-right font-medium">{src.retrieved_at ? fmtTime(src.retrieved_at) : '—'}</dd>
      </dl>
    </aside>
  );
}

function downloadBlob(text, filename, mime) {
  const blob = new Blob([text], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

export default function Workspace() {
  const [prompt, setPrompt] = useState(
    'Find 15 AI startups in London with founders, latest funding, and active engineering roles.'
  );
  const [plan, setPlan] = useState(null);
  const [editing, setEditing] = useState(true);
  const [runId, setRunId] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [sel, setSel] = useState({ record: null, field: null });
  const [q, setQ] = useState('');
  const [sort, setSort] = useState({ col: null, dir: 1 });
  const [busy, setBusy] = useState('');
  const events = useRunStream(runId);

  const doneEvent = events.find((e) => e.type === 'run.completed');
  useEffect(() => {
    const did = doneEvent?.data?.dataset_id;
    if (did) {
      getJSON(`/api/datasets/${did}`)
        .then((r) => setDataset(unwrap(r)))
        .catch(() => {});
    }
  }, [doneEvent]);

  const lastProgress = [...events].reverse().find((e) => typeof e.progress === 'number');
  const progress = lastProgress ? lastProgress.progress : runId ? 2 : 0;

  const cols = useMemo(
    () => (dataset?.schema || []).map((c) => (typeof c === 'string' ? c : c.name)),
    [dataset]
  );
  const rows = useMemo(() => {
    let recs = dataset?.records || [];
    if (q.trim()) {
      const needle = q.trim().toLowerCase();
      recs = recs.filter((r) =>
        cols.some((c) => String(cellValue(cellOf(r, c))).toLowerCase().includes(needle))
      );
    }
    if (sort.col) {
      recs = [...recs].sort((a, b) =>
        String(cellValue(cellOf(a, sort.col))).localeCompare(
          String(cellValue(cellOf(b, sort.col)))
        ) * sort.dir
      );
    }
    return recs;
  }, [dataset, q, sort, cols]);

  async function compile() {
    setBusy('compile');
    try {
      const data = unwrap(await postJSON('/api/workflows/compile', { prompt }));
      setPlan(data);
      setEditing(false);
    } finally {
      setBusy('');
    }
  }
  async function startRun() {
    if (!plan) return;
    setBusy('run');
    try {
      const r = unwrap(await postJSON('/api/runs', { plan_id: plan.plan_id }));
      setRunId(r.run_id);
      setDataset(null);
      setSel({ record: null, field: null });
    } finally {
      setBusy('');
    }
  }
  async function exportAs(fmt) {
    if (!dataset?.id) return;
    setBusy('export-' + fmt);
    try {
      if (fmt === 'json') {
        const r = unwrap(await postJSON(`/api/datasets/${dataset.id}/export`, { format: 'json' }));
        downloadBlob(r.content, 'dataset.json', 'application/json');
      } else {
        const r = await postText(`/api/datasets/${dataset.id}/export`, { format: fmt });
        downloadBlob(r.text, r.filename, fmt === 'csv' ? 'text/csv' : 'text/markdown');
      }
    } finally {
      setBusy('');
    }
  }

  const p = plan?.plan;
  return (
    <div className="mx-auto max-w-[1400px] space-y-5 p-4 lg:p-6">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] font-semibold tracking-[0.18em] text-muted">NEW COLLECTION</p>
        <div className="flex items-center gap-2 text-xs">
          <button
            onClick={() => exportAs('csv')}
            disabled={!dataset || busy === 'export-csv'}
            className="rounded-md border border-rule bg-warm px-3 py-1.5 hover:bg-white disabled:opacity-40"
          >
            ⤓ Export CSV
          </button>
          <button
            onClick={() => exportAs('json')}
            disabled={!dataset || busy === 'export-json'}
            className="rounded-md border border-rule bg-warm px-3 py-1.5 hover:bg-white disabled:opacity-40"
          >
            {'</>'} Export JSON
          </button>
          <button
            onClick={() => exportAs('md')}
            disabled={!dataset || busy === 'export-md'}
            className="rounded-md border border-rule bg-warm px-3 py-1.5 hover:bg-white disabled:opacity-40"
            title="Human-readable report with evidence"
          >
            ▤ Report
          </button>
        </div>
      </div>
      <div className="flex flex-col gap-6 xl:flex-row">
        <div className="min-w-0 flex-1 space-y-5">
          <h1 className="font-display text-3xl lg:text-4xl">What do you want to know?</h1>
          {editing || !p ? (
            <div>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                aria-label="Collection question"
                className="w-full rounded-lg border border-rule bg-warm p-4 text-[15px] shadow-sm"
              />
              <button
                onClick={compile}
                disabled={busy === 'compile' || !prompt.trim()}
                className="mt-3 rounded-md bg-accent px-5 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >
                {busy === 'compile' ? 'Planning…' : 'Build dataset →'}
              </button>
            </div>
          ) : (
            <div>
              <p className="rounded-lg border border-rule bg-warm p-4 text-[15px] shadow-sm">{p.goal}</p>
              <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                <span className="rounded-full border border-rule bg-warm px-3 py-1">▦ {p.requested_count} records</span>
                <span className="rounded-full border border-rule bg-warm px-3 py-1">◫ {p.entity}</span>
                <span className="rounded-full border border-rule bg-warm px-3 py-1">🌐 Public web ▾</span>
                <button onClick={() => setEditing(true)} className="rounded-full border border-rule bg-warm px-3 py-1 hover:bg-white">
                  ✎ Edit plan
                </button>
                {!runId && (
                  <button
                    onClick={startRun}
                    disabled={busy === 'run'}
                    className="rounded-full bg-accent px-4 py-1 font-semibold text-white disabled:opacity-50"
                  >
                    {busy === 'run' ? 'Starting…' : 'Run collection →'}
                  </button>
                )}
              </div>
            </div>
          )}

          {runId && (
            <section aria-label="Run progress">
              <div className="flex items-baseline justify-between">
                <h2 className="font-display text-2xl">Collecting intelligence</h2>
                <span className="text-sm text-muted">{progress}%</span>
              </div>
              <div className="mt-2 h-1 overflow-hidden rounded bg-rule/60" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}>
                <div className="h-full rounded bg-accent transition-[width]" style={{ width: `${progress}%` }} />
              </div>
              <div className="mt-4">
                <Stepper events={events} started={!!runId} />
              </div>
            </section>
          )}

          {dataset && (
            <section aria-label="Dataset" className="rounded-lg border border-rule bg-warm p-4 shadow-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-display text-xl">{dataset.name || 'Collection'}</h2>
                <div className="flex items-center gap-2 text-xs text-muted">
                  <span>{rows.length} records</span>
                  <input
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Filter…"
                    aria-label="Filter records"
                    className="rounded-md border border-rule bg-white px-2 py-1"
                  />
                </div>
              </div>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="text-left text-xs text-muted">
                      <th className="w-8 p-2" aria-label="Select"><span aria-hidden>☐</span></th>
                      {cols.map((c) => (
                        <th key={c} className="p-2 font-medium">
                          <button
                            onClick={() => setSort((s) => ({ col: c, dir: s.col === c ? -s.dir : 1 }))}
                            className="inline-flex items-center gap-1 hover:text-ink"
                            aria-label={`Sort by ${c}`}
                          >
                            {c} <span aria-hidden className="text-[10px]">⇅</span>
                          </button>
                        </th>
                      ))}
                      <th className="p-2 font-medium">Source status</th>
                      <th className="w-10" aria-label="Open proof" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => {
                      const name = String(cellValue(cellOf(r, cols[0])) || `Record ${i + 1}`);
                      const st = recordStatus(r, cols);
                      const active = sel.record === r;
                      return (
                        <tr key={i} className={'border-t border-rule ' + (active ? 'bg-[#EFE7D3]' : 'hover:bg-white/60')}>
                          <td className="p-2 text-muted" aria-hidden>☐</td>
                          {cols.map((c, ci) => {
                            const v = String(cellValue(cellOf(r, c))).slice(0, 60);
                            return (
                              <td key={c} className="p-2">
                                {ci === 0 ? (
                                  <span className="flex items-center gap-2 font-medium">
                                    <span
                                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-[11px] font-bold text-white"
                                      style={{ background: avatarColor(name) }}
                                      aria-hidden
                                    >
                                      {name.slice(0, 1).toUpperCase()}
                                    </span>
                                    <button
                                      className="text-left underline-offset-2 hover:underline"
                                      onClick={() => setSel({ record: r, field: c })}
                                    >
                                      {v}
                                    </button>
                                  </span>
                                ) : (
                                  <button
                                    className="text-left underline-offset-2 hover:underline"
                                    onClick={() => setSel({ record: r, field: c })}
                                  >
                                    {v}
                                  </button>
                                )}
                              </td>
                            );
                          })}
                          <td className="p-2"><StatusPill status={st} /></td>
                          <td className="p-2">
                            <button
                              aria-label={`Open proof for ${name}`}
                              onClick={() => setSel({ record: r, field: cols[0] })}
                              className="rounded p-1 text-muted hover:text-ink"
                            >
                              ›
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {runId && (
            <footer aria-label="Live activity" className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-rule pt-3 text-xs text-muted">
              <span className="inline-flex items-center gap-1.5 font-semibold text-ink">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-600" aria-hidden />Live activity
              </span>
              {events.slice(-4).map((e, i) => (
                <span key={i}>
                  <span className="font-mono">{fmtTime(e.timestamp)}</span>
                  <span className="ml-1.5">{e.message?.slice(0, 70)}</span>
                </span>
              ))}
            </footer>
          )}
        </div>

        <div className="w-full shrink-0 xl:w-[340px]">
          {sel.record && sel.field ? (
            <>
              <div className="hidden xl:block">
                <ProofInspector record={sel.record} field={sel.field} cols={cols} onClose={() => setSel({ record: null, field: null })} />
              </div>
              <div className="fixed inset-0 z-40 bg-black/30 xl:hidden" onClick={() => setSel({ record: null, field: null })}>
                <div className="absolute bottom-0 left-0 right-0 max-h-[85vh] overflow-auto p-3" onClick={(e) => e.stopPropagation()}>
                  <ProofInspector record={sel.record} field={sel.field} cols={cols} onClose={() => setSel({ record: null, field: null })} />
                </div>
              </div>
            </>
          ) : (
            <div className="hidden rounded-lg border border-dashed border-rule p-5 text-sm text-muted xl:block">
              Select any value in the table to inspect its proof — source, evidence quote, and verification status.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
