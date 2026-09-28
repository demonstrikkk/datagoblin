import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import { Kbd, Spinner } from './ui.jsx';
import { when } from '../lib/format.js';

const DESTINATIONS = [
  { id: 'nav-collect', label: 'Collect', hint: 'Start a new research run', go: '/' },
  { id: 'nav-runs', label: 'Runs', hint: 'Browse run history', go: '/runs' },
  { id: 'nav-library', label: 'Library', hint: 'Browse verified datasets', go: '/library' },
  { id: 'nav-intel', label: 'Intel', hint: 'Model comparison and free-model health', go: '/intel' },
];

const score = (needle, hay) => {
  if (!hay) return 0;
  const h = hay.toLowerCase();
  const n = needle.toLowerCase();
  if (h === n) return 1000;
  if (h.startsWith(n)) return 500 - h.length;
  const idx = h.indexOf(n);
  if (idx >= 0) return 200 - idx;
  // subsequence fallback: "ntl" -> "intel"
  let i = 0;
  for (const ch of h) {
    if (ch === n[i]) i += 1;
    if (i === n.length) return 50;
  }
  return 0;
};

export default function CommandPalette({ onClose }) {
  const nav = useNavigate();
  const [q, setQ] = useState('');
  const [runs, setRuns] = useState(null);
  const [datasets, setDatasets] = useState(null);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
    const ctrl = new AbortController();
    Promise.allSettled([
      api.history({ signal: ctrl.signal }),
      api.datasets({ signal: ctrl.signal }),
    ]).then(([r, d]) => {
      if (ctrl.signal.aborted) return;
      if (r.status === 'fulfilled') setRuns(Array.isArray(r.value) ? r.value : r.value?.items || []);
      if (d.status === 'fulfilled') setDatasets(Array.isArray(d.value) ? d.value : d.value?.items || []);
    });
    return () => ctrl.abort();
  }, []);

  const results = useMemo(() => {
    const out = [];
    for (const d of DESTINATIONS) {
      out.push({
        ...d,
        group: 'Go to',
        text: `${d.label} ${d.hint}`,
        weight: score(q, d.label) || (q ? 0 : 100),
      });
    }
    (runs || []).forEach((r) => {
      const id = r.run_id || r.id;
      out.push({
        id: `run-${id}`,
        label: String(id).slice(0, 10),
        hint: r.query ? String(r.query).slice(0, 60) : r.status,
        group: 'Runs',
        go: `/runs/${id}`,
        meta: `${r.status || ''} · ${when(r.created_at)}`,
        text: `${id} ${r.query || ''}`,
        weight: score(q, String(id)) + score(q, r.query) * 0.5,
      });
    });
    (datasets || []).forEach((d) => {
      const id = d.dataset_id || d.id;
      out.push({
        id: `ds-${id}`,
        label: d.name || d.query || String(id).slice(0, 10),
        hint: d.goal ? String(d.goal).slice(0, 60) : null,
        group: 'Datasets',
        go: `/library/${id}`,
        meta: `${d.record_count ?? d.count ?? 0} records`,
        text: `${d.name || ''} ${id} ${d.query || ''} ${d.goal || ''}`,
        weight: score(q, d.name) + score(q, String(id)) * 0.5,
      });
    });

    const filtered = out.filter((r) => r.weight > 0).sort((a, b) => b.weight - a.weight);
    return q ? filtered.slice(0, 12) : filtered.slice(0, 10);
  }, [q, runs, datasets]);

  useEffect(() => setCursor(0), [q]);

  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-i="${cursor}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [cursor]);

  const go = (r) => {
    if (!r) return;
    nav(r.go);
    onClose();
  };

  const onKey = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      go(results[cursor]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  let lastGroup = null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-ink/20 px-4 pt-[12vh] backdrop-blur-[2px] animate-fade-in"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-full max-w-[560px] overflow-hidden rounded-lg border border-rule bg-paper-2 shadow-deep animate-scale-in"
      >
        <div className="flex items-center gap-2.5 border-b border-rule px-3.5">
          <span className="text-muted" aria-hidden="true">
            ⌕
          </span>
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKey}
            placeholder="Jump to a run, dataset, or view…"
            aria-label="Search"
            className="h-11 flex-1 bg-transparent text-[13.5px] text-ink outline-none placeholder:text-muted/70"
          />
          <Kbd>esc</Kbd>
        </div>

        <div ref={listRef} className="scroll-y max-h-[46vh] p-1.5">
          {results.length === 0 ? (
            <p className="px-3 py-6 text-center text-[12.5px] text-muted">
              Nothing matches “{q}”.
            </p>
          ) : (
            results.map((r, i) => {
              const header = r.group !== lastGroup ? r.group : null;
              lastGroup = r.group;
              return (
                <div key={r.id}>
                  {header ? (
                    <p className="eyebrow px-2.5 pb-1 pt-2.5">{header}</p>
                  ) : null}
                  <button
                    type="button"
                    data-i={i}
                    onMouseEnter={() => setCursor(i)}
                    onClick={() => go(r)}
                    className={`row flex w-full items-center gap-2.5 rounded-sm px-2.5 py-2 text-left ${
                      i === cursor ? 'bg-warm' : ''
                    }`}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12.5px] text-ink">{r.label}</span>
                      {r.hint ? (
                        <span className="block truncate text-[11px] text-muted">{r.hint}</span>
                      ) : null}
                    </span>
                    {r.meta ? (
                      <span className="shrink-0 font-mono text-[10.5px] text-muted">{r.meta}</span>
                    ) : null}
                  </button>
                </div>
              );
            })
          )}
        </div>

        <div className="flex items-center justify-between border-t border-rule px-3.5 py-2 text-[10.5px] text-muted">
          <span className="flex items-center gap-2">
            <Kbd>↑</Kbd>
            <Kbd>↓</Kbd> navigate <Kbd>↵</Kbd> open
          </span>
          {runs === null || datasets === null ? (
            <span className="flex items-center gap-1.5">
              <Spinner /> indexing
            </span>
          ) : (
            <span>{results.length} results</span>
          )}
        </div>
      </div>
    </div>
  );
}
