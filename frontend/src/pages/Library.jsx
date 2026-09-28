import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import {
  Dot,
  Empty,
  ErrorNote,
  Loading,
  Notice,
  Pill,
  StackedBar,
  Stat,
} from '../components/ui.jsx';
import { num, truncate, when } from '../lib/format.js';

/**
 * Datasets are the product. Show quality, not just a count.
 *
 * Two generations of `counts` live in this database, and the older one is all
 * zeros for many runs. Absent aggregate counters are NOT evidence of absent
 * verification: the per-field `verification_status` is stored on every record
 * regardless. So this returns `counted: false` in that case, and the card says
 * "not counted" rather than asserting "unproven" — which would be a confident
 * negative the data does not support.
 */
function qualityOf(ds) {
  const c = ds?.counts || {};
  const proven = Number(c.fields_verified) || 0;
  const unjudged = Number(c.fields_judgment_unavailable) || 0;
  const throttled = Number(c.fields_rate_limited) || 0;
  const records = Number(c.records) || Number(ds?.record_count) || 0;
  const total = proven + unjudged + throttled;

  const rows = [];
  if (proven) rows.push({ key: 'verified', n: proven, tone: 'ok', label: 'Proven fields' });
  if (unjudged) rows.push({ key: 'unjudged', n: unjudged, tone: 'info', label: 'Unjudged fields' });
  if (throttled) rows.push({ key: 'throttled', n: throttled, tone: 'judge', label: 'Rate limited' });

  return { rows, total, records, counted: total > 0, verifiedRecords: Number(c.records_fully_verified) || 0 };
}

export default function Library() {
  const { data, status, error, refetch } = useResource((o) => api.datasets(o), []);
  const { inspect } = useInspector();
  const [q, setQ] = useState('');

  const datasets = useMemo(() => (Array.isArray(data) ? data : []), [data]);

  const shown = useMemo(() => {
    const n = q.trim().toLowerCase();
    if (!n) return datasets;
    return datasets.filter((d) =>
      [d.name, d.id, d.run_id].filter(Boolean).some((v) => String(v).toLowerCase().includes(n))
    );
  }, [datasets, q]);

  const totals = useMemo(() => {
    const acc = { records: 0, proven: 0, unjudged: 0, throttled: 0, uncounted: 0 };
    datasets.forEach((d) => {
      const x = qualityOf(d);
      acc.records += x.records;
      acc.proven += x.rows.find((r) => r.key === 'verified')?.n || 0;
      acc.unjudged += x.rows.find((r) => r.key === 'unjudged')?.n || 0;
      acc.throttled += x.rows.find((r) => r.key === 'throttled')?.n || 0;
      if (!x.counted) acc.uncounted += 1;
    });
    return acc;
  }, [datasets]);

  if (status === 'loading' && !datasets.length) return <Loading rows={5} label="Loading datasets" />;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="h-display text-[26px] leading-tight text-ink">Library</h2>
          <p className="mt-1 text-[12.5px] text-muted">
            Verified datasets, each traceable back to the pages that produced it.
          </p>
        </div>
        <button type="button" onClick={refetch} className="btn-outline btn-xs">
          Refresh
        </button>
      </header>

      {error ? <ErrorNote error={error} onRetry={refetch} /> : null}

      {datasets.length ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Datasets" value={num(datasets.length)} />
            <Stat label="Records" value={num(totals.records)} />
            <Stat
              label="Counted verification"
              value={`${datasets.length - totals.uncounted}/${datasets.length}`}
              tone={totals.uncounted === datasets.length ? 'muted' : 'ok'}
              sub={
                totals.uncounted
                  ? `${num(totals.uncounted)} without counters`
                  : 'all runs reported it'
              }
            />
            <Stat
              label="Proven fields"
              value={totals.proven ? num(totals.proven) : '—'}
              tone={totals.proven ? 'ok' : 'muted'}
              sub={
                totals.unjudged || totals.throttled
                  ? `${num(totals.unjudged + totals.throttled)} unproven`
                  : totals.proven
                  ? 'all proven'
                  : 'no counters written'
              }
            />
          </div>

          {shown.length === 0 ? (
            <div className="surface">
              <Empty title="No datasets match" hint="Clear the filter to see everything." />
            </div>
          ) : (
            <>
              {/* Stated once, not on every card. When no run wrote counters,
                  repeating the explanation 10 times says nothing 9 times too
                  many. */}
              {totals.uncounted === shown.length ? (
                <Notice tone="muted">
                  None of these runs wrote per-field verification counters, so no quality bar is
                  shown. The per-field status is still stored on every record — open a dataset to
                  read it.
                </Notice>
              ) : totals.uncounted ? (
                <Notice tone="muted">
                  {num(totals.uncounted)} of {num(shown.length)} shown did not write verification
                  counters, so they carry no quality bar. Per-field status is still stored on every
                  record.
                </Notice>
              ) : null}

              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 stagger">
                {shown.map((d) => {
                  const x = qualityOf(d);
                  const allProven = x.counted && x.rows.every((r) => r.key === 'verified');
                  return (
                    <Link
                      key={d.id}
                      to={`/library/${d.id}`}
                      className="surface group flex flex-col p-3.5 transition-all duration-200 ease-swift hover:border-accent/40 hover:shadow-lift focusable"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <h3 className="min-w-0 flex-1 text-[13.5px] font-medium leading-snug text-ink transition-colors group-hover:text-accent">
                          <span className="line-clamp-2">{d.name || `Dataset ${String(d.id).slice(0, 8)}`}</span>
                        </h3>
                        {x.counted ? (
                          allProven ? (
                            <Pill tone="ok" glyph="✓">
                              all proven
                            </Pill>
                          ) : (
                            <Pill tone="warn" glyph="◐">
                              mixed
                            </Pill>
                          )
                        ) : null}
                      </div>

                      <p className="mt-1 font-mono text-[10px] text-muted">
                        {String(d.id).slice(0, 8)} · {when(d.created_at)}
                      </p>

                      {x.counted ? (
                        <div className="mt-3">
                          <StackedBar rows={x.rows} total={x.total} height={6} />
                          <ul className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5">
                            {x.rows.map((r) => (
                              <li key={r.key} className="flex items-center gap-1 text-[10.5px] text-muted">
                                <Dot tone={r.tone} />
                                <span className="font-mono tnum text-ink-2">{num(r.n)}</span>
                                {r.label.toLowerCase()}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}

                      <footer className="mt-auto flex items-baseline justify-between gap-2 border-t border-rule pt-2.5">
                        <span className="font-mono text-[17px] leading-none text-ink tnum">
                          {num(d.record_count ?? x.records)}
                        </span>
                        <span className="truncate text-[10.5px] text-muted">
                          {d.schema?.length
                            ? `${d.schema.length} fields`
                            : 'schema not persisted'}
                          {x.verifiedRecords ? ` · ${num(x.verifiedRecords)} fully proven` : ''}
                        </span>
                      </footer>
                    </Link>
                  );
                })}
              </div>
            </>
          )}
        </>
      ) : !error ? (
        <div className="surface">
          <Empty
            icon="▤"
            title="The library is empty"
            hint="Datasets appear here once a run stores records. A run that finds nothing will tell you why instead of writing an empty dataset."
            action={
              <Link to="/" className="btn-accent">
                Start a run
              </Link>
            }
          />
        </div>
      ) : null}

      {datasets.length ? (
        <p className="text-[11px] text-muted">
          Showing {shown.length} of {datasets.length}.{' '}
          <button
            type="button"
            onClick={() => inspect({ kind: 'event', event: { type: 'note', datasets }, title: 'Raw dataset list' })}
            className="link"
          >
            Inspect raw payload
          </button>
        </p>
      ) : null}
    </div>
  );
}
