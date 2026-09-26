import { useState } from 'react';
import { postJSON, unwrap } from '../lib/api.js';
export function PromptComposer({ onPlan }) {
  const [prompt, setPrompt] = useState('Find 15 AI startups in London with founders, latest funding and active engineering roles.');
  return (
    <div className="border rule rounded p-6 bg-white">
      <h1 className="font-display text-3xl">Real questions. Verified answers.</h1>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4} className="w-full mt-4 border rule rounded p-3" />
      <button className="mt-3 px-4 py-2 rounded text-white" style={{ background: '#A77A32' }}
        onClick={async () => onPlan(unwrap(await postJSON('/api/workflows/compile', { prompt })))}>Build dataset →</button>
    </div>
  );
}
export function PlanPreview({ plan, onRun }) {
  if (!plan?.plan) return null;
  const p = plan.plan;
  return (
    <div className="border rule rounded p-6 bg-white mt-4">
      <h2 className="font-display text-xl">Your collection plan</h2>
      <p className="text-sm mt-1">Target: {p.entity} · {p.requested_count} records · {p.search_queries?.length} queries</p>
      <ul className="text-sm mt-2">{(p.fields || []).map((f) => <li key={f.name}>✓ {f.name} ({f.type})</li>)}</ul>
      <button className="mt-3 px-4 py-2 border rule rounded" onClick={() => onRun(plan)}>Run collection</button>
    </div>
  );
}
export function RunTimeline({ events }) {
  const stages = ['PLANNING','DISCOVERING','FETCHING','REDUCING','EXTRACTING','VALIDATING','DEDUPLICATING','FINALIZING','COMPLETED'];
  const seen = new Set(events.map((e) => e.stage));
  return <ol className="text-sm">{stages.map((s) => <li key={s}>{seen.has(s) ? '✓' : '○'} {s}</li>)}</ol>;
}
export function SourceList({ events }) {
  return <ul className="text-sm">{events.filter((e) => e.type?.startsWith('source.')).map((e, i) => <li key={i}>{e.message}</li>)}</ul>;
}
export function ActivityFeed({ events }) {
  return <ul className="text-xs font-mono">{events.map((e, i) => <li key={i}>{e.timestamp} · {e.stage} · {e.message}</li>)}</ul>;
}
export function DatasetTable({ dataset, onCell }) {
  const rows = dataset?.records || [];
  const cols = (dataset?.schema || []).map((c) => (typeof c === 'string' ? c : c.name));
  return (
    <table className="w-full text-sm border rule">
      <thead><tr>{cols.map((c) => <th key={c} className="border rule p-2 text-left">{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => {
        const cell = r.wrapped?.[c] ?? r.fields?.[c];
        const v = cell?.value ?? cell ?? '';
        return <td key={c} className="border rule p-2 cursor-pointer" onClick={() => onCell(cell, c)}>{String(v).slice(0, 80)}</td>;
      })}</tr>)}</tbody>
    </table>
  );
}
export function ProofDrawer({ cell, field, onClose }) {
  if (!cell) return null;
  const src = cell.source || {};
  return (
    <aside className="fixed right-0 top-0 h-full w-80 bg-white border-l rule p-5 overflow-auto">
      <button onClick={onClose}>✕</button>
      <h3 className="font-display text-xl mt-2">{field}</h3>
      <p className="text-lg">{String(cell.value ?? '')}</p>
      <p className="text-xs">{cell.verification_status}</p>
      <p className="text-sm mt-3">{src.title}</p>
      <a className="text-xs underline" href={src.url} target="_blank" rel="noreferrer">{src.url}</a>
      <blockquote className="text-sm mt-3 border-l-2 pl-3">“{src.quote}”</blockquote>
      <p className="text-xs mt-2">{src.retrieved_at}</p>
      <a className="text-sm underline" href={src.url} target="_blank" rel="noreferrer">Open source ↗</a>
    </aside>
  );
}
export function FilterBar({ q, setQ }) { return <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search..." className="border rule rounded p-2" />; }
export function DatasetToolbar({ onExport }) {
  return <div><button className="border rule rounded px-3 py-1" onClick={() => onExport('csv')}>CSV</button><button className="border rule rounded px-3 py-1 ml-2" onClick={() => onExport('json')}>JSON</button></div>;
}
export function ColumnHeader({ name }) { return <span>{name}</span>; }
export function HistoryList({ items }) {
  return <ul>{items.map((h) => <li key={h.run_id} className="border-b rule py-2 text-sm">{h.status} · {h.run_id}</li>)}</ul>;
}
export function ExportMenu({ onExport }) { return <DatasetToolbar onExport={onExport} />; }
