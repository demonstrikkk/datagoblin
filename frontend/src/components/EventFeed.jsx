import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { EVENT_META, truncate, when } from '../lib/format.js';
import { useInspector } from '../lib/inspector.jsx';
import { Dot, Pill, Segmented } from './ui.jsx';

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'flow', label: 'Flow' }, // stage + run lifecycle
  { value: 'sources', label: 'Sources' },
  { value: 'records', label: 'Records' },
  { value: 'problems', label: 'Problems' }, // rejected / conflicts / rate limits
];

const bucket = (type) => {
  if (type.startsWith('record.') || type === 'duplicate.merged') return 'records';
  if (type.startsWith('source.')) return 'sources';
  if (type === 'run.partial' || type === 'record.rejected' || type === 'run.failed') return 'problems';
  return 'flow';
};

/**
 * What a single event line should say, in plain language.
 *
 * `progress` arrives 0–100, the same as the run view. Treating it as a
 * fraction rendered a 90% stage as "9000%"; a 0–1 value is still accepted for
 * safety, but a bare 0 is not, since 0 is a legitimate 0%.
 */
const asPercent = (p) => {
  const n = Number(p);
  if (!Number.isFinite(n)) return null;
  if (n > 0 && n <= 1) return Math.round(n * 100); // fraction form
  return Math.round(n);
};

function describe(e) {
  switch (e.type) {
    case 'stage.started':
      return e.detail || `${e.stage} started`;
    case 'stage.progress': {
      const p = asPercent(e.progress);
      if (e.detail) return e.detail;
      return p === null ? 'progressing' : `${p}%`;
    }
    case 'source.discovered':
      return e.url || e.domain || e.title || 'candidate source';
    case 'source.fetched':
      return `${e.url || e.domain || 'source'}${e.char_count ? ` · ${e.char_count} chars` : ''}`;
    case 'source.reused':
      // A distinct kind rather than folded into `source.fetched`, so the feed
      // shows that nothing was requested for this URL.
      return `${e.url || 'source'} — reused, not re-fetched${
        e.retrieved_at ? ` (${e.retrieved_at})` : ''
      }`;
    case 'record.extracted':
      return e.qualifier || e.value || e.title || 'record extracted';
    case 'record.verified':
      return `${e.qualifier || 'record'} verified`;
    case 'record.needs_review':
      return `${e.qualifier || 'record'} — ${e.reason || 'needs human review'}`;
    case 'record.rejected':
      return `${e.qualifier || 'record'} — ${e.reason || 'failed validation'}`;
    case 'duplicate.merged': {
      const kept = e.kept ?? e.kept_id ?? e.survivor;
      const dropped = e.merged ?? e.dropped ?? e.removed;
      // Never print a bare "?" — an event that carries no merge detail should
      // say what it knows, not render a placeholder.
      if (kept && dropped) return `kept ${truncate(String(kept), 28)} ← merged ${dropped}`;
      if (dropped) return `merged ${dropped} duplicate${dropped === 1 ? '' : 's'}`;
      if (kept) return `kept ${truncate(String(kept), 40)}`;
      return e.detail || e.message || 'duplicate merged';
    }
    case 'run.completed':
      return e.dataset_id ? `dataset ${String(e.dataset_id).slice(0, 8)}` : 'run completed';
    case 'run.failed':
      return e.error || e.reason || e.message || 'run failed';
    case 'run.cancelled':
      return 'cancelled by operator';
    case 'run.partial':
      return e.reason || e.message || e.no_yield_reason || 'partial result saved';
    default:
      return e.detail || e.reason || e.message || '';
  }
}

export default function EventFeed({ events = [], connection, height = 380, emptyHint }) {
  const [filter, setFilter] = useState('all');
  const [follow, setFollow] = useState(true);
  const [paused, setPaused] = useState(false);
  const { inspect } = useInspector();
  const boxRef = useRef(null);
  const stickRef = useRef(true);

  const shown = events.filter((e) => (filter === 'all' ? true : bucket(e.type) === filter));

  const counts = {
    all: events.length,
    flow: events.filter((e) => bucket(e.type) === 'flow').length,
    sources: events.filter((e) => bucket(e.type) === 'sources').length,
    records: events.filter((e) => bucket(e.type) === 'records').length,
    problems: events.filter((e) => bucket(e.type) === 'problems').length,
  };

  // Only auto-scroll when the user is already at the bottom, so reading
  // history is never yanked away by an incoming event.
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el || !follow || paused || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [shown.length, follow, paused]);

  useEffect(() => {
    if (connection === 'open' && paused) setPaused(false);
  }, [connection, paused]);

  return (
    <div className="flex min-h-0 flex-col">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <Segmented
          options={FILTERS.map((f) => ({ ...f, count: counts[f.value] }))}
          value={filter}
          onChange={setFilter}
          size="xs"
        />
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 text-[10.5px] text-muted">
            {connection === 'open' ? (
              <>
                <Dot tone="ok" live /> live
              </>
            ) : connection === 'reconnecting' ? (
              <>
                <Dot tone="warn" live /> reconnecting
              </>
            ) : connection === 'closed' ? (
              <>
                <Dot tone="muted" /> closed
              </>
            ) : connection === 'connecting' ? (
              <span className="text-muted">connecting…</span>
            ) : (
              <span className="text-muted">idle</span>
            )}
          </span>
          <button
            type="button"
            onClick={() => setFollow((v) => !v)}
            disabled={!events.length}
            className={`btn-xs rounded-xs border px-1.5 py-0.5 text-[10.5px] transition-colors ${
              follow
                ? 'border-accent/30 bg-warn-soft text-accent'
                : 'border-rule text-muted hover:text-ink'
            }`}
            title={follow ? 'Following new events' : 'Scroll paused'}
          >
            {follow ? 'following' : 'paused'}
          </button>
        </div>
      </div>

      <div
        ref={boxRef}
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="scroll-y surface-inset min-h-0 flex-1 overflow-y-auto"
        style={{ height }}
        role="log"
        aria-live="polite"
        aria-label="Run event stream"
      >
        {shown.length === 0 ? (
          <p className="px-3 py-8 text-center text-[12px] text-muted">
            {events.length === 0 ? emptyHint || 'Waiting for the first event…' : 'No events in this filter.'}
          </p>
        ) : (
          <ul className="divide-y divide-rule/70">
            {shown.map((e, i) => {
              const meta = EVENT_META[e.type] || { tone: 'muted', label: e.type };
              return (
                <li key={`${e.type}-${i}`}>
                  <button
                    type="button"
                    onClick={() => inspect({ kind: 'event', event: e, title: e.type })}
                    className="row flex w-full items-start gap-2 px-2.5 py-[6px] text-left"
                  >
                    <span className="mt-[5px] shrink-0">
                      <Dot tone={meta.tone} />
                    </span>
                    <span className="w-[86px] shrink-0 truncate font-mono text-[10px] text-muted">
                      {e.stage || meta.label}
                    </span>
                    <span className="min-w-0 flex-1 text-[11.5px] leading-snug text-ink-2">
                      {truncate(describe(e), 140)}
                    </span>
                    <span className="shrink-0 font-mono text-[9.5px] text-muted/70">
                      {when(e.timestamp || e.ts)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
