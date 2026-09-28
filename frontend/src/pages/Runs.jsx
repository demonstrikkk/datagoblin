import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import { useRunContext } from '../lib/run-context.jsx';
import {
  Dot,
  Empty,
  ErrorNote,
  Loading,
  Pill,
  Segmented,
  StackedBar,
  Stat,
} from '../components/ui.jsx';
import {
  num,
  runCounters,
  runId,
  runRecordTotal,
  runStatusMeta,
  truncate,
  verifySummary,
  when,
} from '../lib/format.js';

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'live', label: 'Running' },
  { value: 'done', label: 'Complete' },
  { value: 'partial', label: 'Partial' },
  { value: 'failed', label: 'Failed' },
];

const matches = (r, f) => {
  const s = r?.status;
  const partial = Boolean(r?.partial) || s === 'PARTIAL';
  if (f === 'all') return true;
  if (f === 'live') return s === 'RUNNING' || s === 'PLANNING';
  if (f === 'done') return s === 'COMPLETED' && !partial;
  if (f === 'partial') return partial;
  if (f === 'failed') return (s === 'FAILED' || s === 'CANCELLED') && !partial;
  return true;
};

export default function Runs() {
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const { data, status, error, refetch, isStale } = useResource((o) => api.history(o), [], {
    pollMs: 8000,
  });
  const { inspect } = useInspector();
  const { setActiveRun } = useRunContext();
  const nav = useNavigate();

  const runs = useMemo(() => (Array.isArray(data) ? data : []), [data]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return runs.filter((r) => {
      if (!matches(r, filter)) return false;
      if (!needle) return true;
      return [runId(r), r.query, r.goal, r.status, r.current_stage, r.dataset_id]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle));
    });
  }, [runs, filter, q]);

  const counts = useMemo(
    () => ({
      all: runs.length,
      live: runs.filter((r) => matches(r, 'live')).length,
      done: runs.filter((r) => matches(r, 'done')).length,
      partial: runs.filter((r) => matches(r, 'partial')).length,
      failed: runs.filter((r) => matches(r, 'failed')).length,
    }),
    [runs]
  );

  const totals = useMemo(() => {
    const acc = { records: 0, proven: 0, unproven: 0, conflicting: 0, throttled: 0, judged: 0 };
    runs.forEach((r) => {
      const c = runCounters(r);
      acc.records += runRecordTotal(r);
      acc.proven += Number(c.fields_verified) || 0;
      acc.unproven += Number(c.fields_unverified) || 0;
      acc.judged += Number(c.fields_judgment_unavailable) || 0;
      acc.conflicting += Number(c.fields_conflicting) || 0;
      acc.throttled += Number(c.fields_rate_limited) || 0;
    });
    acc.fields = acc.proven + acc.unproven + acc.judged + acc.conflicting + acc.throttled;
    return acc;
  }, [runs]);

  if (status === 'loading' && !runs.length) return <Loading rows={8} label="Loading runs" />;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="h-display text-[26px] leading-tight text-ink">Runs</h2>
          <p className="mt-1 text-[12.5px] text-muted">
            Every execution this database remembers, newest first.
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={refetch} className="btn-outline btn-xs">
            Refresh
          </button>
          <button type="button" onClick={() => nav('/')} className="btn-accent btn-xs">
            New run
          </button>
        </div>
      </header>

      {error ? <ErrorNote error={error} onRetry={refetch} /> : null}

      {runs.length ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Total runs" value={num(runs.length)} />
            <Stat label="Records" value={num(totals.records)} sub="across all runs" />
            <Stat
              label="Fields proven"
              value={totals.fields ? num(totals.proven) : '—'}
              tone={totals.proven ? 'ok' : 'muted'}
              sub={
                totals.fields
                  ? `${Math.round((totals.proven / totals.fields) * 100)}% of ${num(totals.fields)} fields`
                  : 'no counters recorded'
              }
            />
            <Stat
              label="Not proven"
              value={totals.fields ? num(totals.unproven + totals.judged) : '—'}
              tone={totals.unproven + totals.judged ? 'warn' : 'muted'}
              sub={
                totals.conflicting
                  ? `${num(totals.conflicting)} conflicting · ${num(totals.throttled)} throttled`
                  : totals.fields
                  ? 'unverified or unjudged'
                  : 'no counters recorded'
              }
            />
          </div>

          <div className="surface overflow-hidden">
            <header className="flex flex-wrap items-center justify-between gap-2 border-b border-rule px-3.5 py-2.5">
              <Segmented
                options={FILTERS.map((f) => ({ ...f, count: counts[f.value] }))}
                value={filter}
                onChange={setFilter}
                size="xs"
              />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Filter runs…"
                aria-label="Filter runs"
                className="input-sm input w-full max-w-[220px]"
              />
            </header>

            {isStale ? (
              <div className="border-b border-rule bg-warm/40 px-3.5 py-1 text-[10.5px] text-muted">
                refreshing…
              </div>
            ) : null}

            {shown.length === 0 ? (
              <Empty
                title="No runs match"
                hint={
                  runs.length
                    ? 'Try a different filter or clear the search.'
                    : 'Start one from the Collect view.'
                }
                action={
                  runs.length ? (
                    <button
                      type="button"
                      onClick={() => {
                        setFilter('all');
                        setQ('');
                      }}
                      className="btn-outline btn-xs"
                    >
                      Clear filters
                    </button>
                  ) : (
                    <Link to="/" className="btn-accent btn-xs">
                      Start a run
                    </Link>
                  )
                }
              />
            ) : (
              <div className="scroll-y overflow-x-auto">
                <table className="w-full min-w-[720px] border-collapse text-left">
                  <thead>
                    <tr className="border-b border-rule">
                      {['Status', 'Request', 'Stage', 'Records', 'Proven', 'When', ''].map((h) => (
                        <th key={h} className="eyebrow px-3.5 py-2 font-semibold">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-rule">
                    {shown.map((r, i) => {
                      const meta = runStatusMeta(r);
                      const c = runCounters(r);
                      const v = verifySummary(r);
                      return (
                        <tr
                          key={runId(r) || i}
                          className="row"
                          style={{ animation: `rise-in .3s var(--e-swift) both`, animationDelay: `${Math.min(i, 12) * 18}ms` }}
                        >
                          <td className="px-3.5 py-2 align-top">
                            <Pill tone={meta.tone} glyph={meta.glyph} live={meta.live}>
                              {meta.label}
                            </Pill>
                          </td>
                          <td className="max-w-[280px] px-3.5 py-2 align-top">
                            <button
                              type="button"
                              onClick={() => inspect({ kind: 'run', run: r, json: r })}
                              className="focusable block max-w-full truncate text-left text-[12.5px] text-ink hover:text-accent"
                              title={r.query || r.goal || ''}
                            >
                              {truncate(r.query || r.goal || runId(r), 64)}
                            </button>
                            <span className="font-mono text-[10px] text-muted">
                              {String(runId(r)).slice(0, 8)}
                            </span>
                          </td>
                          <td className="px-3.5 py-2 align-top">
                            <span className="font-mono text-[11px] text-muted">
                              {r.current_stage || '—'}
                            </span>
                          </td>
                          <td className="px-3.5 py-2 align-top font-mono text-[12px] tnum text-ink">
                            {num(runRecordTotal(r))}
                          </td>
                          <td className="w-[132px] px-3.5 py-2 align-top">
                            {v?.total ? (
                              <>
                                <StackedBar rows={v.rows} total={v.total} height={5} />
                                <span
                                  className="mt-1 block font-mono text-[10px] text-muted"
                                  title={`${num(c.fields_verified)} of ${num(v.total)} fields judge-verified`}
                                >
                                  {num(c.fields_verified)}/{num(v.total)} fields proven
                                </span>
                              </>
                            ) : (
                              <span className="text-[11px] text-rule-2" title="No field-level counters recorded">
                                —
                              </span>
                            )}
                          </td>
                          <td className="px-3.5 py-2 align-top">
                            <span className="text-[11.5px] text-muted">
                              {when(r.started_at || r.created_at)}
                            </span>
                          </td>
                          <td className="px-3.5 py-2 align-top text-right">
                            <Link
                              to={`/runs/${runId(r)}`}
                              onClick={() => setActiveRun(runId(r))}
                              className="btn-ghost btn-xs"
                            >
                              Open →
                            </Link>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      ) : !error ? (
        <div className="surface">
          <Empty
            icon="⌁"
            title="No runs yet"
            hint="Describe what you need in plain English. The planner shows you the schema and source strategy before anything is fetched."
            action={
              <Link to="/" className="btn-accent">
                Start your first run
              </Link>
            }
          />
        </div>
      ) : null}
    </div>
  );
}
