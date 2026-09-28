import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, downloadBlob } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import {
  CopyButton,
  Dot,
  Empty,
  ErrorNote,
  Loading,
  Notice,
  Pill,
  Segmented,
  Select,
  Skeleton,
  Spinner,
  StackedBar,
  Stat,
  Toggle,
} from '../components/ui.jsx';
import { REC_VERIFY, bytes, host, num, toneClass, truncate, when } from '../lib/format.js';

const PAGE = 50;

/* ---------------------------------------------------------------- records */

function Cell({ prov }) {
  const pf = prov && typeof prov === 'object' ? prov : null;
  const raw = pf ? pf.value : prov;
  const status = pf?.verification_status;
  const m = status ? REC_VERIFY[status] : null;

  const text =
    raw === null || raw === undefined || raw === ''
      ? '—'
      : typeof raw === 'object'
      ? Array.isArray(raw)
        ? raw.join(', ')
        : JSON.stringify(raw)
      : String(raw);

  return (
    <span className="flex min-w-0 items-start gap-1.5">
      <span
        className={`mt-[3px] shrink-0 ${m ? toneClass(m.tone).split(' ')[0] : 'text-rule-2'}`}
        title={m?.hint || 'No judge assessment for this field'}
        aria-hidden="true"
      >
        {m ? m.glyph : '·'}
      </span>
      <span className="sr-only">{m ? m.label : 'unjudged'}: </span>
      <span className="truncate" title={text}>
        {truncate(text, 120)}
      </span>
    </span>
  );
}

function RecordsTable({ datasetId, schema, onPick }) {
  const [page, setPage] = useState(0);
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');

  useEffect(() => {
    const id = setTimeout(() => {
      setDebounced(q);
      setPage(0);
    }, 300);
    return () => clearTimeout(id);
  }, [q]);

  const { data, status, error, refetch, isStale } = useResource(
    (o) => api.records(datasetId, { limit: PAGE, offset: page * PAGE, q: debounced }, o),
    [datasetId, page, debounced]
  );

  // The endpoint returns `{dataset_id, total, records}` — not a bare array.
  // `total` is the whole dataset, which is what makes real pagination possible.
  const rows = useMemo(() => data?.records || [], [data]);
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  // The stored `schema` is empty on datasets written before it was persisted,
  // so columns are derived from the rows themselves. That is the honest source:
  // these are the fields that actually exist in storage.
  const columns = useMemo(() => {
    const fromSchema = (schema || []).map((f) => f.name || f.field || f).filter(Boolean);
    if (fromSchema.length) return fromSchema;
    const seen = new Set();
    rows.forEach((r) => Object.keys(r?.fields || {}).forEach((k) => seen.add(k)));
    return [...seen];
  }, [schema, rows]);

  const counts = useMemo(() => {
    const c = {
      all: rows.length,
      verified: 0,
      judgment_unavailable: 0,
      rate_limited: 0,
      conflicting: 0,
    };
    rows.forEach((r) => {
      const fs = Object.values(r?.fields || {});
      if (fs.length && fs.every((f) => f?.verification_status === 'verified')) c.verified += 1;
      if (fs.some((f) => f?.verification_status === 'judgment_unavailable'))
        c.judgment_unavailable += 1;
      if (fs.some((f) => f?.verification_status === 'rate_limited')) c.rate_limited += 1;
      if (fs.some((f) => f?.verification_status === 'conflicting')) c.conflicting += 1;
    });
    return c;
  }, [rows]);

  const shown = useMemo(() => {
    if (statusFilter === 'all') return rows;
    // Status filters run over the current page only — the API filters on `q`,
    // not on verification status, so claiming a dataset-wide count here would
    // be a lie. The footer says "filtered to N on this page" for that reason.
    if (statusFilter === 'proven')
      return rows.filter((r) => {
        const fs = Object.values(r?.fields || {});
        return fs.length && fs.every((f) => f?.verification_status === 'verified');
      });
    return rows.filter((r) =>
      Object.values(r?.fields || {}).some((f) => f?.verification_status === statusFilter)
    );
  }, [rows, statusFilter]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented
          size="xs"
          value={statusFilter}
          onChange={setStatusFilter}
          options={[
            { value: 'all', label: 'All', count: counts.all },
            { value: 'proven', label: 'Fully proven', count: counts.verified },
            { value: 'judgment_unavailable', label: 'Unjudged', count: counts.judgment_unavailable },
            { value: 'rate_limited', label: 'Throttled', count: counts.rate_limited },
            { value: 'conflicting', label: 'Conflicting', count: counts.conflicting },
          ].filter((o) => o.value === 'all' || o.count > 0)}
        />
        <div className="flex items-center gap-1.5">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search records…"
            aria-label="Search records"
            className="input-sm input w-44"
          />
          <button type="button" onClick={refetch} className="btn-outline btn-xs">
            Refresh
          </button>
        </div>
      </div>

      {error ? <ErrorNote error={error} onRetry={refetch} /> : null}

      <div className="surface overflow-hidden">
        {status === 'loading' && !rows.length ? (
          <div className="space-y-2 p-3.5" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="flex gap-3">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="h-3 flex-1" />
                <Skeleton className="h-3 w-16" />
              </div>
            ))}
          </div>
        ) : !shown.length ? (
          <Empty
            title={rows.length ? 'No records match this filter' : 'No records stored'}
            hint={
              rows.length
                ? 'Status filters apply to the rows on this page. Switch back to All to see every one.'
                : 'This dataset has no stored rows.'
            }
          />
        ) : !columns.length ? (
          <Empty title="Records have no fields" hint="Stored rows are empty payloads." />
        ) : (
          <>
            <div className="scroll-y overflow-x-auto">
              <table className="w-full min-w-max border-collapse text-left">
                <thead>
                  <tr className="border-b border-rule">
                    {columns.map((c) => (
                      <th key={c} className="eyebrow whitespace-nowrap px-3.5 py-2 font-semibold">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-rule">
                  {shown.map((r, i) => (
                    <tr
                      key={i}
                      onClick={() => onPick(r, page * PAGE + rows.indexOf(r) + 1)}
                      className="row cursor-pointer text-[12px] text-ink-2"
                      style={{
                        animation: `rise-in .3s var(--e-swift) both`,
                        animationDelay: `${Math.min(i, 14) * 14}ms`,
                      }}
                    >
                      {columns.map((c) => (
                        <td key={c} className="max-w-[280px] px-3.5 py-2 align-top">
                          <Cell prov={r?.fields?.[c]} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-rule px-3.5 py-2">
              <span className="text-[11px] text-muted">
                {isStale ? 'refreshing… · ' : ''}
                showing {(page * PAGE + 1).toLocaleString()}–
                {(page * PAGE + rows.length).toLocaleString()} of{' '}
                <span className="font-mono tnum text-ink-2">{total.toLocaleString()}</span>
                {statusFilter !== 'all' ? (
                  <span className="text-warn"> · filtered to {shown.length} on this page</span>
                ) : null}
              </span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  className="btn-outline btn-xs"
                >
                  ← Prev
                </button>
                <span className="px-1 font-mono text-[10.5px] text-muted">
                  {page + 1}/{pages}
                </span>
                <button
                  type="button"
                  disabled={page + 1 >= pages}
                  onClick={() => setPage((p) => p + 1)}
                  className="btn-outline btn-xs"
                >
                  Next →
                </button>
              </div>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- sources */

function SourceGrid({ datasetId }) {
  const { data, status, error, refetch } = useResource(
    (o) => api.sources(datasetId, o),
    [datasetId]
  );
  const { inspect } = useInspector();

  // Returns `{dataset_id, sources_attempted, sources_successful, sources_failed, sources[]}`.
  const sources = useMemo(() => data?.sources || [], [data]);
  const attempted = data?.sources_attempted ?? null;
  const successful = data?.sources_successful ?? null;
  const failed = data?.sources_failed ?? null;

  if (status === 'loading' && !data) return <Loading rows={4} label="Loading sources" />;
  if (error) return <ErrorNote error={error} onRetry={refetch} />;
  if (!sources.length) {
    return (
      <Empty
        title="No sources recorded"
        hint="Sources appear when a run stores pages against this dataset."
      />
    );
  }

  const byHost = new Map();
  sources.forEach((s) => {
    const h = host(s.url || s.page_url);
    if (!byHost.has(h)) byHost.set(h, []);
    byHost.get(h).push(s);
  });

  const fetchRate =
    attempted && successful !== null ? successful / attempted : null;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Pages stored"
          value={num(sources.length)}
          sub={attempted !== null ? `${num(attempted)} attempted` : undefined}
        />
        <Stat
          label="Domains"
          value={num(byHost.size)}
          sub={fetchRate !== null ? `${pctSafe(fetchRate)} fetch success` : undefined}
        />
        <Stat
          label="Failed fetches"
          value={num(failed ?? 0)}
          tone={failed ? 'danger' : 'ok'}
          sub={failed ? 'permitted but unsuccessful' : 'none failed'}
        />
      </div>

      <div className="space-y-3">
        {[...byHost.entries()].map(([h, list]) => {
          const ok = list.filter((s) => s.status === 'ok').length;
          return (
            <section key={h} className="surface overflow-hidden">
              <header className="flex items-center justify-between gap-2 border-b border-rule px-3.5 py-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Dot tone={ok === list.length ? 'ok' : ok ? 'warn' : 'danger'} />
                  <span className="truncate font-mono text-[12px] text-ink">{h}</span>
                </div>
                <span className="shrink-0 text-[11px] text-muted">
                  {list.length} page{list.length === 1 ? '' : 's'}
                </span>
              </header>
              <ul className="divide-y divide-rule">
                {list.map((s, i) => {
                  const url = s.url || s.page_url;
                  const good = s.status === 'ok';
                  return (
                    <li key={`${s.content_hash || url}-${i}`}>
                      <div className="row flex items-center gap-2.5 px-3.5 py-2">
                        <span
                          className={`shrink-0 ${good ? 'text-ok' : 'text-danger'}`}
                          title={good ? 'Fetched and stored' : s.error || 'Failed'}
                          aria-label={good ? 'Fetched' : 'Failed'}
                        >
                          {good ? '✓' : '✕'}
                        </span>
                        <div className="min-w-0 flex-1">
                          <a
                            href={url}
                            target="_blank"
                            rel="noreferrer noopener"
                            onClick={(e) => e.stopPropagation()}
                            className="block truncate text-[12px] text-ink-2 hover:text-accent hover:underline"
                            title={url}
                          >
                            {s.title || truncate(url, 90)}
                          </a>
                          {s.content_hash ? (
                            <span
                              className="font-mono text-[9.5px] text-muted"
                              title="Content hash — re-fetch and confirm the page has not changed"
                            >
                              {String(s.content_hash).slice(0, 12)}
                            </span>
                          ) : null}
                        </div>
                        <button
                          type="button"
                          onClick={() => inspect({ kind: 'source', source: s, json: s })}
                          className="btn-ghost btn-xs shrink-0"
                        >
                          Inspect
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- export */

function ExportPanel({ datasetId, schema }) {
  const [format, setFormat] = useState('csv');
  const [fields, setFields] = useState(() => (schema || []).map((f) => f.name || f.field).filter(Boolean));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(null);

  const toggle = (name) =>
    setFields((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]));

  const go = async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const res = await api.exportDataset(datasetId, {
        format,
        ...(format !== 'json' && fields.length ? { fields } : {}),
      });
      const name = `dataset-${String(datasetId).slice(0, 8)}.${format}`;
      if (format === 'json') {
        // JSON comes back enveloped, not as an attachment.
        const blob = new Blob([JSON.stringify(res?.content ?? res, null, 2)], {
          type: 'application/json',
        });
        downloadBlob(blob, name);
      } else {
        downloadBlob(res.blob, name);
      }
      setDone(format.toUpperCase());
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <Notice tone="muted">
        Every export is billed work and is written to the run ledger, so repeated downloads are
        recorded rather than free. CSV and Markdown stream back as file attachments; JSON returns
        the same content inline.
      </Notice>

      <div className="surface space-y-4 p-3.5">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="label">Format</span>
            <Select
              value={format}
              onChange={(e) => setFormat(e.target.value)}
              options={[
                { value: 'csv', label: 'CSV — spreadsheet ready' },
                { value: 'md', label: 'Markdown — human readable' },
                { value: 'json', label: 'JSON — full provenance' },
              ]}
            />
          </label>
          <div>
            <span className="label">Fields included</span>
            <p className="text-[11.5px] text-muted">
              {format === 'json'
                ? 'JSON always carries every field and its full evidence chain.'
                : fields.length
                ? `${fields.length} of ${schema?.length || 0} fields`
                : 'None selected — the server will use its default set.'}
            </p>
          </div>
        </div>

        {format !== 'json' && schema?.length ? (
          <div className="flex flex-wrap gap-1.5">
            {schema.map((f) => {
              const name = f.name || f.field;
              if (!name) return null;
              const on = fields.includes(name);
              return (
                <button
                  key={name}
                  type="button"
                  onClick={() => toggle(name)}
                  className={`rounded-xs border px-2 py-1 font-mono text-[11px] transition-all duration-150 ease-swift ${
                    on
                      ? 'border-accent/40 bg-warn-soft text-accent'
                      : 'border-rule bg-warm/50 text-muted hover:text-ink'
                  }`}
                >
                  {on ? '✓ ' : ''}
                  {name}
                </button>
              );
            })}
          </div>
        ) : null}

        {error ? <ErrorNote error={error} /> : null}

        <div className="flex items-center gap-2">
          <button type="button" onClick={go} disabled={busy} className="btn-accent">
            {busy ? (
              <>
                <Spinner /> Exporting…
              </>
            ) : (
              `Download ${format.toUpperCase()}`
            )}
          </button>
          {done ? (
            <span className="text-[12px] text-ok animate-fade-in">
              ✓ {done} downloaded
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- page */

export default function DatasetPage() {
  const { id } = useParams();
  const { inspect } = useInspector();
  const [tab, setTab] = useState('records');

  const { data, status, error, refetch } = useResource((o) => api.dataset(id, o), [id]);

  if (status === 'loading' && !data) return <Loading rows={7} label="Loading dataset" />;

  if (error) {
    return (
      <div className="space-y-4">
        <ErrorNote error={error} onRetry={refetch} />
        <Link to="/library" className="btn-outline btn-xs">
          ← Back to library
        </Link>
      </div>
    );
  }

  if (!data) return <Empty title="Dataset not found" />;

  /*
   * Two generations of `counts` exist in this database.
   *
   * Current runs write field-level detail: `fields_verified`,
   * `fields_judgment_unavailable`, `fields_rate_limited`,
   * `records_fully_verified`. Older datasets only have the coarse
   * `Counters` shape: `records`, `verified`, `needs_review`.
   *
   * Both are handled, and when only the coarse shape is present the UI says
   * so rather than rendering an empty quality bar that reads as "no data".
   */
  const c = data?.counts || {};
  const modern = 'fields_verified' in c || 'records_fully_verified' in c;
  const proven = Number(c.fields_verified) || 0;
  const unjudged = Number(c.fields_judgment_unavailable) || 0;
  const throttled = Number(c.fields_rate_limited) || 0;
  const fieldTotal = proven + unjudged + throttled;

  const qrows = [
    proven && { key: 'verified', n: proven, tone: 'ok', label: 'Proven' },
    unjudged && { key: 'unjudged', n: unjudged, tone: 'info', label: 'Unjudged' },
    throttled && { key: 'throttled', n: throttled, tone: 'judge', label: 'Throttled' },
  ].filter(Boolean);

  return (
    <div className="space-y-5">
      <header className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="eyebrow">Dataset</p>
            <h2 className="mt-1 h-display text-[26px] leading-tight text-ink balance">
              {data.name || `Dataset ${String(id).slice(0, 8)}`}
            </h2>
            <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[10.5px] text-muted">
              <span>{String(id).slice(0, 12)}</span>
              <span>created {when(data.created_at)}</span>
              {data.run_id ? (
                <Link to={`/runs/${data.run_id}`} className="link">
                  run {String(data.run_id).slice(0, 8)}
                </Link>
              ) : null}
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <CopyButton value={id} label="Copy ID" />
            <button
              type="button"
              onClick={() => inspect({ kind: 'event', event: data, title: 'Raw dataset' })}
              className="btn-ghost btn-xs"
            >
              Raw
            </button>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Records" value={num(data.record_count)} />
          {modern ? (
            <>
              <Stat
                label="Records fully proven"
                value={num(c.records_fully_verified)}
                tone={c.records_fully_verified ? 'ok' : 'muted'}
                sub="every field verified"
              />
              <Stat
                label="Proven fields"
                value={num(proven)}
                tone={proven ? 'ok' : 'muted'}
                sub={unjudged ? `${num(unjudged)} unjudged` : 'all judged'}
              />
            </>
          ) : (
            <>
              <Stat
                label="Verified records"
                value={num(c.verified)}
                tone={c.verified ? 'ok' : 'muted'}
                title="Coarse count recorded by an older run"
              />
              <Stat
                label="Needing review"
                value={num(c.needs_review)}
                tone={c.needs_review ? 'warn' : 'muted'}
              />
            </>
          )}
          <Stat
            label="Schema"
            value={data.schema?.length ? num(data.schema.length) : '—'}
            sub={data.schema?.length ? 'fields' : 'not persisted'}
          />
        </div>

        {fieldTotal ? (
          <div className="surface p-3">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <p className="eyebrow">Evidence quality</p>
              <span className="text-[11px] text-muted">
                {pctSafe(proven / fieldTotal)} of fields carry judge-verified quotes
              </span>
            </div>
            <StackedBar rows={qrows} total={fieldTotal} height={8} />
            <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
              {qrows.map((r) => (
                <li key={r.key} className="flex items-center gap-1.5 text-[11px] text-muted">
                  <Dot tone={r.tone} />
                  <span className="text-ink-2">{r.label}</span>
                  <span className="font-mono tnum text-ink">{num(r.n)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <Notice tone="warn">
            <span className="font-medium">No field-level verification was recorded for this run.</span>{' '}
            The run predates per-field judging, so aggregate counts are all that exist. Open any
            record to see the per-field status that <em>is</em> stored with each value.
          </Notice>
        )}
      </header>

      <Segmented
        value={tab}
        onChange={setTab}
        options={[
          { value: 'records', label: 'Records' },
          { value: 'sources', label: 'Sources' },
          { value: 'export', label: 'Export' },
        ]}
      />

      {tab === 'records' ? (
        <RecordsTable
          datasetId={id}
          schema={data.schema}
          onPick={(record, index) =>
            inspect({
              kind: 'record',
              record,
              title: `Record ${index + 1}`,
              sub: data.name,
              json: record,
            })
          }
        />
      ) : null}

      {tab === 'sources' ? <SourceGrid datasetId={id} /> : null}
      {tab === 'export' ? <ExportPanel datasetId={id} schema={data.schema} /> : null}
    </div>
  );
}

const pctSafe = (n) => (Number.isFinite(n) ? `${Math.round(n * 100)}%` : '—');
