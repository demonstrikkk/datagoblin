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
      ? 'â€”'
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
        {m ? m.glyph : 'Â·'}
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

  // The endpoint returns `{dataset_id, total, records}` â€” not a bare array.
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
    // Status filters run over the current page only â€” the API filters on `q`,
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
            placeholder="Search recordsâ€¦"
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
          // "No records stored" and "no records matched your search" are
          // different facts, and conflating them told a user their run had
          // failed when the run was fine and their query was simply too narrow.
          <Empty
            title={
              rows.length
                ? 'No records match this filter'
                : debounced
                ? `No record contains "${truncate(debounced, 40)}"`
                : 'No records stored'
            }
            hint={
              rows.length
                ? 'Status filters apply to the rows on this page. Switch back to All to see every one.'
                : debounced
                ? 'The dataset has records, but none match that text. Clear the search to see them.'
                : 'This dataset has no stored rows. Open the run view for the reason it produced none.'
            }
            action={
              rows.length || debounced ? (
                <button
                  type="button"
                  onClick={() => {
                    setQ('');
                    setDebounced('');
                    setPage(0);
                    setStatusFilter('all');
                  }}
                  className="btn-outline btn-xs"
                >
                  Clear search and filters
                </button>
              ) : null
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
                {isStale ? 'refreshingâ€¦ Â· ' : ''}
                showing {(page * PAGE + 1).toLocaleString()}â€“
                {(page * PAGE + rows.length).toLocaleString()} of{' '}
                <span className="font-mono tnum text-ink-2">{total.toLocaleString()}</span>
                {statusFilter !== 'all' ? (
                  <span className="text-warn"> Â· filtered to {shown.length} on this page</span>
                ) : null}
              </span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  disabled={page === 0}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  className="btn-outline btn-xs"
                >
                  â† Prev
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
                  Next â†’
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

function SelectorLearner({ runId, neverExtracted }) {
  const { data: pages } = useResource((o) => api.runPages(runId, o), [runId]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState(null);
  const [failure, setFailure] = useState(null);
  const [saved, setSaved] = useState(null);
  const [overwrite, setOverwrite] = useState(false);

  if (!neverExtracted.length) return null;

  // Learn from a page this run actually stored. Re-fetching would cost another
  // request and could teach selectors from a different rendering than the one
  // extraction will see.
  const page = (pages?.pages || [])[0];
  const domain = page ? host(page.url) : '';

  async function propose() {
    if (!page) return;
    setBusy(true);
    setFailure(null);
    setSaved(null);
    try {
      const res = await api.proposeSelectors({
        url: page.url,
        page_id: page.id,
        fields: neverExtracted.map((f) => ({ name: f, type: 'string' })),
      });
      setDraft(res);
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  async function persist() {
    setBusy(true);
    setFailure(null);
    try {
      const res = await api.saveSelectors(
        { domain: draft.domain, page_url: draft.page_url, provider: draft.provider, fields: draft.fields },
        { overwrite }
      );
      setSaved(res);
      setDraft(null);
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Learn selectors</h3>
          <p className="text-xs text-muted">
            {neverExtracted.length} field{neverExtracted.length === 1 ? '' : 's'} nothing
            extracted.{' '}
            {domain
              ? `Proposed CSS is checked against a stored ${domain} page and only offered if it returns real text.`
              : 'No stored page to learn from â€” run a collection first.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
            disabled={!page || busy}
            onClick={propose}
          >
            {busy ? 'Workingâ€¦' : 'Propose selectors'}
          </button>
        </div>
      </div>

      {saved ? (
        <Notice tone="ok">
          Saved {saved.fields} field selector{saved.fields === 1 ? '' : 's'} for{' '}
          {saved.domain}{saved.replaced ? ' (replaced the previous schema).' : '.'}
        </Notice>
      ) : null}
      {failure ? <ErrorNote error={failure} compact onRetry={propose} /> : null}

      {draft ? (
        <div className="space-y-2 rounded-lg border border-rule-2/60 p-3">
          <div className="text-xs text-muted">
            {Object.keys(draft.fields || {}).length} of {neverExtracted.length} verified
            against {draft.page_url} Â· via {draft.provider}
          </div>
          {Object.entries(draft.fields || {}).map(([name, spec]) => (
            <div key={name} className="border-b border-rule-2/40 py-1.5 last:border-0">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-sm">{name}</span>
                {spec.multiple ? <Pill tone="info">multiple</Pill> : null}
              </div>
              <code className="mt-1 block text-xs text-muted">{spec.selectors.join(' , ')}</code>
              <div className="mt-1 text-xs">
                yields:{' '}
                <span className="text-ink">{Object.values(spec.samples || {})[0] || 'â€”'}</span>
              </div>
            </div>
          ))}
          {(draft.rejected || []).length ? (
            <div className="space-y-1 pt-1">
              {(draft.rejected || []).map((r, i) => (
                <p key={i} className="text-xs text-muted">
                  {r.field}: {r.reason}
                  {(r.selectors || (r.selector ? [r.selector] : [])).length ? (
                    <span className="block opacity-80">
                      tried: {(r.selectors || [r.selector]).join(' , ')}
                    </span>
                  ) : null}
                </p>
              ))}
            </div>
          ) : null}
          {draft.usable ? (
            <div className="flex items-center gap-3 pt-1">
              <label className="flex items-center gap-1.5 text-xs text-muted">
                <input
                  type="checkbox"
                  checked={overwrite}
                  onChange={(e) => setOverwrite(e.target.checked)}
                />
                replace any existing schema for {draft.domain}
              </label>
              <button
                type="button"
                className="ml-auto rounded border border-rule-2 px-2 py-1 text-xs"
                disabled={busy}
                onClick={persist}
              >
                Check in
              </button>
            </div>
          ) : (
            <Notice tone="warn">
              Nothing verified, so nothing is offered. Saving this would replace
              extraction with selectors known not to work.
            </Notice>
          )}
        </div>
      ) : null}
    </div>
  );
}

function CoveragePanel({ datasetId, runId }) {
  const { data, status, error, refetch } = useResource(
    (o) => api.coverage(datasetId, o),
    [datasetId]
  );

  if (status === 'loading' && !data) return <Loading rows={5} label="Reading stored records" />;
  if (error) return <ErrorNote error={error} onRetry={refetch} />;
  if (!data) return <Empty title="No coverage data" hint="Coverage appears once records are stored." />;

  const fields = data.fields || [];
  const backlog = data.backlog?.items || [];
  const total = data.records || 0;
  const complete = fields.filter((f) => f.missing === 0).length;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Records" value={num(total)} sub="rows scanned" />
        <Stat label="Fields declared" value={num(fields.length)} sub={`${complete} on every record`} />
        <Stat
          label="Open conflicts"
          value={num(data.backlog?.open_conflicts ?? 0)}
          sub="sources disagree"
        />
      </div>

      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-medium">Per-field coverage</h3>
          <span className="text-xs text-muted">
            Present is not the same as proven â€” the darker slice is values carrying a verdict.
          </span>
        </div>
        {fields.map((f) => (
          <div
            key={f.field}
            className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-rule-2/60 px-3 py-2"
          >
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <span className="truncate text-sm">{f.field}</span>
                <span className="shrink-0 text-xs tabular-nums text-muted">
                  {f.present}/{f.records}
                </span>
              </div>
              <StackedBar
                className="mt-1.5"
                rows={[
                  { key: 'verified', n: f.verified, label: 'verified' },
                  { key: 'unverified', n: f.unverified, label: 'no verdict' },
                  { key: 'conflicting', n: f.conflicting, label: 'conflicting' },
                  { key: 'missing', n: f.missing, label: 'absent' },
                ]}
                total={f.records}
              />
            </div>
            <span className="shrink-0 text-xs tabular-nums text-muted">
              {f.coverage_pct}%
            </span>
          </div>
        ))}
      </div>

      <SelectorLearner
        runId={runId}
        neverExtracted={fields.filter((f) => f.present === 0).map((f) => f.field)}
      />

      {backlog.length ? (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">What is outstanding</h3>
          <p className="text-xs text-muted">
            Ranked by how much work each gap represents. A field no stored page
            carried is a schema problem, not a re-run problem.
          </p>
          {backlog.map((b) => (
            <div
              key={b.field}
              className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule-2/40 py-1.5 last:border-0"
            >
              <span className="text-sm">{b.field}</span>
              <span className="flex-1 text-xs text-muted">{b.reason}</span>
              <Pill tone={b.routable ? 'warn' : 'muted'}>
                {b.outstanding} open
              </Pill>
            </div>
          ))}
        </div>
      ) : (
        <Notice tone="ok">
          Every declared field is filled on every record.
        </Notice>
      )}
    </div>
  );
}

function ConflictsPanel({ datasetId }) {
  const { data, status, error, refetch } = useResource(
    (o) => api.conflicts(datasetId, o),
    [datasetId]
  );
  const [busy, setBusy] = useState(null);
  const [failure, setFailure] = useState(null);
  // Above the early returns on purpose: a hook declared after them is skipped
  // on the loading render and present on the loaded one, which React reports as
  // "rendered more hooks than during the previous render" and which blanks the
  // whole panel.
  // Resolved conflicts stop coming back from the server — that is what
  // "resolved" means. So decided cards are kept from a local snapshot;
  // rendering only the server list made a card vanish at the moment the user
  // confirmed it, which reads as the action having failed.
  const [resolved, setResolved] = useState({});

  if (status === 'loading' && !data) return <Loading rows={4} label="Reading disputes" />;
  if (error) return <ErrorNote error={error} onRetry={refetch} />;

  const conflicts = data?.conflicts || [];
  const keyOf = (c) => `${c.record_id}|${c.field}`;
  const live = conflicts.filter((c) => !resolved[keyOf(c)]);
  const rows = [...live, ...Object.values(resolved).map((r) => r.conflict)];
  if (!conflicts.length && !Object.keys(resolved).length) {
    return (
      <Empty
        title="No disagreements between sources"
        hint="When two pages give the same field different values, both sides appear here with their quotes."
      />
    );
  }

  async function decide(c, choice, rivalIndex) {
    const key = keyOf(c);
    setBusy(key);
    setFailure(null);
    try {
      await api.resolveConflict(datasetId, {
        record_id: c.record_id,
        field: c.field,
        choice,
        rival_index: rivalIndex,
      });
      setResolved((d) => ({
        ...d,
        [key]: {
          conflict: c,
          outcome: choice === 'keep' ? 'kept the first value' : `adopted rival ${Number(rivalIndex) + 1}`,
        },
      }));
      // Re-read: the open-conflict count just changed, and leaving the old
      // number on screen next to a card saying it was resolved is two truths
      // that disagree.
      refetch();
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
      {live.length === 0 ? (
        <Notice tone="ok">
          All {conflicts.length} disagreement{conflicts.length === 1 ? '' : 's'} resolved.
          The decisions are stored on the records.
        </Notice>
      ) : (
        <p className="text-xs text-muted">
          {live.length} open. Each side quotes the page it came from. Keeping the
          incumbent or adopting a rival both preserve evidence â€” there is no way
          to type in a value that was never extracted.
        </p>
      )}

      {failure ? <ErrorNote error={failure} onRetry={refetch} /> : null}

      {rows.slice(0, 40).map((c) => {
        const key = keyOf(c);
        const done = Boolean(resolved[key]);
        const outcome = resolved[key]?.outcome;
        return (
          <div
            key={key}
            className={`space-y-2 rounded-lg border p-3 ${done ? 'border-ok/40 opacity-70' : 'border-rule-2/60'}`}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium">{c.field}</span>
              <span className="text-xs text-muted">record {c.record_id}</span>
            </div>
            {done ? (
              <Notice tone="ok">
                Resolved â€” {outcome}.
              </Notice>
            ) : null}
            <div className="rounded-md border border-warn/40 bg-warn/5 p-2">
              <div className="text-xs uppercase tracking-wide text-muted">incumbent</div>
              <div className="text-sm">{String(c.incumbent.value ?? 'â€”')}</div>
              {c.incumbent.quote ? (
                <div className="mt-1 border-l-2 border-rule-2 pl-2 text-xs text-muted">
                  â€œ{c.incumbent.quote}â€
                </div>
              ) : null}
              <div className="mt-2">
                <button
                  type="button"
                  className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
                  disabled={busy === key || done}
                  onClick={() => decide(c, 'keep')}
                >
                  Keep this
                </button>
              </div>
            </div>
            {c.rivals.map((rv) => (
              <div key={rv.index} className="rounded-md border border-rule-2/60 p-2">
                <div className="text-xs uppercase tracking-wide text-muted">
                  rival {rv.index + 1}
                  {rv.url ? ` Â· ${host(rv.url)}` : ''}
                </div>
                <div className="text-sm">{String(rv.value ?? 'â€”')}</div>
                {rv.quote ? (
                  <div className="mt-1 border-l-2 border-rule-2 pl-2 text-xs text-muted">
                    â€œ{rv.quote}â€
                  </div>
                ) : null}
                <div className="mt-2">
                  <button
                    type="button"
                    className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
                    disabled={busy === key || done}
                    onClick={() => decide(c, 'adopt', rv.index)}
                  >
                    Adopt this
                  </button>
                </div>
              </div>
            ))}
          </div>
        );
      })}
      {conflicts.length > 40 ? (
        <p className="text-xs text-muted">
          Showing the first 40 of {conflicts.length}.
        </p>
      ) : null}
    </div>
  );
}

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
                          {good ? 'âœ“' : 'âœ•'}
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
                              title="Content hash â€” re-fetch and confirm the page has not changed"
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
                { value: 'csv', label: 'CSV â€” spreadsheet ready' },
                { value: 'md', label: 'Markdown â€” human readable' },
                { value: 'json', label: 'JSON â€” full provenance' },
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
                : 'None selected â€” the server will use its default set.'}
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
                  {on ? 'âœ“ ' : ''}
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
                <Spinner /> Exportingâ€¦
              </>
            ) : (
              `Download ${format.toUpperCase()}`
            )}
          </button>
          {done ? (
            <span className="text-[12px] text-ok animate-fade-in">
              âœ“ {done} downloaded
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
          â† Back to library
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
            value={data.schema?.length ? num(data.schema.length) : 'â€”'}
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
          { value: 'coverage', label: 'Coverage' },
          { value: 'conflicts', label: 'Conflicts' },
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

      {tab === 'coverage' ? <CoveragePanel datasetId={id} runId={data.run_id} /> : null}
      {tab === 'conflicts' ? <ConflictsPanel datasetId={id} /> : null}
      {tab === 'sources' ? <SourceGrid datasetId={id} /> : null}
      {tab === 'export' ? <ExportPanel datasetId={id} schema={data.schema} /> : null}
    </div>
  );
}

const pctSafe = (n) => (Number.isFinite(n) ? `${Math.round(n * 100)}%` : 'â€”');
