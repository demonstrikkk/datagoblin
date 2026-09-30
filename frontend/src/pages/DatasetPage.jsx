import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, downloadBlob } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import FieldsView from '../components/FieldsView.jsx';
import GapQueue from '../components/GapQueue.jsx';
import SectorPanel from '../components/SectorPanel.jsx';
import ReviewQueue from '../components/ReviewQueue.jsx';
import { CellStatus, asField, evidenceDepth } from '../components/evidence.jsx';
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
import {
  bytes,
  host,
  num,
  numericShare,
  toneVar,
  topCategories,
  truncate,
  when,
} from '../lib/format.js';

const PAGE = 50;

/* ---------------------------------------------------------------- records */

/**
 * One cell in the ledger.
 *
 * A cell is a button, because a cell is a claim and the product's whole purpose
 * is to let a reader interrogate a claim. Clicking the cell opens the evidence
 * rail on that one field; clicking the row opens the record. Two targets, two
 * questions — "why is this value like that" and "what is on this record" — and
 * the cell wins the click when it is under the pointer.
 *
 * A cell with nothing behind it still renders, but is not a button. Offering to
 * open an evidence drawer that will report no evidence is worse than not
 * offering, because it teaches the reader that the affordance is unreliable.
 */
function Cell({ name, prov, onOpen }) {
  const pf = asField(prov);
  const raw = pf.value;
  const text =
    raw === null || raw === undefined || raw === ''
      ? '—'
      : typeof raw === 'object'
      ? Array.isArray(raw)
        ? raw.join(', ')
        : JSON.stringify(raw)
      : String(raw);

  const openable = evidenceDepth(pf.source) > 0;
  const body = (
    <>
      <CellStatus status={pf.status} />
      <span className="sr-only">{name}: </span>
      <span className="truncate" title={text}>
        {truncate(text, 120)}
      </span>
    </>
  );

  if (!openable) {
    return (
      <span className="flex min-w-0 items-start gap-1.5 text-muted">
        <CellStatus status={pf.status} />
        <span className="sr-only">{name}: </span>
        <span className="truncate" title={text}>
          {truncate(text, 120)}
        </span>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={(e) => {
        // The row is also clickable and this button is inside it.
        e.stopPropagation();
        onOpen();
      }}
      className="focusable group/cell -mx-1 flex min-w-0 max-w-full items-start gap-1.5 rounded-xs px-1 text-left transition-colors duration-150 ease-swift hover:bg-accent/10"
      title={`${name} — show the evidence behind this value`}
    >
      {body}
      {/* The rail affordance. Faint until the row is under the pointer, so a
          dense table is not a field of arrows. */}
      <span
        aria-hidden="true"
        className="mt-px shrink-0 text-[9px] text-accent opacity-0 transition-opacity duration-150 group-hover/cell:opacity-100 group-focus-visible/cell:opacity-100"
      >
        ⇥
      </span>
    </button>
  );
}

function RecordsTable({ datasetId, schema, onPick, onCell }) {
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
          label="Record status filter"
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
              {/* The ledger. Thin rules, generous rows, and no card shadow — a
                  research table is ruled paper, not a set of stacked panels.
                  Row height is deliberately loose so a claim and the glyph that
                  qualifies it are readable without zooming. */}
              <table className="w-full min-w-max border-collapse text-left">
                <thead className="sticky top-0 z-10 bg-paper-2/95 backdrop-blur">
                  <tr className="border-b border-rule-2">
                    {columns.map((c) => (
                      <th
                        key={c}
                        className="eyebrow whitespace-nowrap px-3.5 py-2.5 font-semibold"
                        scope="col"
                      >
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
                      className="row cursor-pointer text-[12px] text-ink-2 hover:text-ink"
                      style={{
                        animation: `rise-in .3s var(--e-swift) both`,
                        animationDelay: `${Math.min(i, 14) * 14}ms`,
                      }}
                    >
                      {columns.map((c) => (
                        <td key={c} className="max-w-[280px] px-3.5 py-2.5 align-top leading-snug">
                          <Cell
                            name={c}
                            prov={r?.fields?.[c]}
                            onOpen={() => onCell(r, c, page * PAGE + rows.indexOf(r) + 1)}
                          />
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

function BackfillPanel({ datasetId }) {
  const [proposal, setProposal] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const [loaded, setLoaded] = useState(false);
  // Top-up is a separate, explicit choice rather than a default. A field filled
  // on some records and empty on others is the commonest gap, but widening the
  // default would quietly change what "Backfill" means for everyone who used it
  // before. Offered, labelled with its real cell count, never assumed.
  const [includePartial, setIncludePartial] = useState(false);

  const load = useCallback(
    (partial) =>
      api.backfillProposal(datasetId, { limit_pages: 6, include_partial: partial }),
    [datasetId]
  );

  useEffect(() => {
    let alive = true;
    // A read, so it can load on mount rather than waiting for a click. The cost
    // is one query and no model call: the proposal is arithmetic over stored
    // records, and it is the number the user needs before deciding anything.
    load(includePartial)
      .then((p) => { if (alive) { setProposal(p); setLoaded(true); } })
      .catch((e) => { if (alive) { setFailure(e); setLoaded(true); } });
    return () => { alive = false; };
  }, [datasetId, includePartial, load]);

  async function run(apply) {
    setBusy(true);
    setFailure(null);
    try {
      setResult(
        await api.backfill(datasetId, { apply, limit_pages: 6, include_partial: includePartial })
      );
      if (apply) {
        // Coverage and the backlog both just changed.
        const p = await load(includePartial);
        setProposal(p);
        setResult((r) => ({ ...r }));
      }
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;
  if (failure && !proposal) return <ErrorNote error={failure} compact />;

  const p = proposal || {};
  const partialCount = Object.keys(p.partial || {}).length;

  if (!p.fields?.length) {
    return (
      <div className="space-y-2">
        <Notice tone={partialCount ? 'info' : 'ok'}>
          {partialCount
            ? `Nothing is entirely absent, but ${partialCount} field${
                partialCount === 1 ? ' is' : 's are'
              } filled on some records and empty on others.`
            : 'Nothing to backfill — every declared field already has a value on at least one record.'}
        </Notice>
        {partialCount > 0 && (
          <button
            type="button"
            className="rounded border border-ink px-2 py-1 text-xs"
            disabled={busy}
            onClick={() => setIncludePartial(true)}
          >
            Top up the empty cells
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">
            {includePartial ? 'Top up empty cells from stored pages' : 'Backfill from stored pages'}
          </h3>
          <p className="text-xs text-muted">
            {p.fields.length} field{p.fields.length === 1 ? '' : 's'}
            {includePartial
              ? ' with empty cells: '
              : ' no record carries: '}
            <span className="font-mono">{p.fields.join(', ')}</span>
            {p.empty_cells ? (
              <>
                {' '}
                — {p.empty_cells} empty cell{p.empty_cells === 1 ? '' : 's'} in
                total.
              </>
            ) : null}{' '}
            Re-reads {p.pages} page{p.pages === 1 ? '' : 's'} this run already
            stored — nothing is crawled — and fills only empty cells.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted">
            <input
              type="checkbox"
              checked={includePartial}
              onChange={(e) => { setResult(null); setIncludePartial(e.target.checked); }}
            />
            Include partly filled columns
          </label>
          <button
            type="button"
            disabled={busy || !p.backfillable}
            className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
            onClick={() => run(false)}
          >
            {busy ? 'Working…' : 'Check what would fill'}
          </button>
          <button
            type="button"
            disabled={busy || !p.backfillable}
            className="rounded border border-ink px-2 py-1 text-xs disabled:opacity-40"
            onClick={() => run(true)}
          >
            {includePartial ? 'Top them up' : 'Fill them'}
          </button>
        </div>
      </div>

      {!p.backfillable && p.reason ? (
        <Notice tone="warn">{p.reason}</Notice>
      ) : (
        <p className="text-xs text-muted">
          Matched by {p.dedupe_keys?.join(' + ')} — exact only, so a value can
          never be attached to the wrong record. A cell that already holds a value
          is never overwritten, in either mode. About{' '}
          {p.estimated_extractions} extraction
          {p.estimated_extractions === 1 ? '' : 's'} and up to{' '}
          {p.estimated_judge_calls} verification calls.
          {p.top_hosts?.length ? (
            <>
              {' '}
              Pages are ranked by how many of your own records they name, with
              recorded yield from {p.top_hosts.slice(0, 3).join(', ')} as the
              tiebreaker.
            </>
          ) : null}
        </p>
      )}

      {failure ? <ErrorNote error={failure} compact onRetry={() => run(false)} /> : null}

      {result ? (
        <div className="space-y-1 rounded-lg border border-rule-2/60 p-2 text-xs">
          <div>
            Read {result.pages_read} stored page{result.pages_read === 1 ? '' : 's'},
            matched {result.records_matched} record
            {result.records_matched === 1 ? '' : 's'}.
          </div>
          <div>
            <strong>{result.filled ?? result.fillable}</strong> value
            {(result.filled ?? result.fillable) === 1 ? '' : 's'} found
            {result.applied ? ' — written' : ' — nothing written (this was a check)'}.
          </div>
          {result.filled_fields && Object.keys(result.filled_fields).length ? (
            <div className="text-muted">
              {Object.entries(result.filled_fields)
                .map(([k, v]) => `${k}: ${v}`).join(' · ')}
            </div>
          ) : null}
          {result.unmatched ? (
            <div className="text-muted">
              {result.unmatched} extraction
              {result.unmatched === 1 ? '' : 's'} matched no record and were
              left alone. Reported, never guessed.
            </div>
          ) : null}
          {result.applied ? (
            <button
              type="button"
              className="mt-1 text-[11px] text-muted underline underline-offset-2"
              onClick={() => { setResult(null); window.location.reload(); }}
            >
              Reload to see updated coverage
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

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
              : 'No stored page to learn from — run a collection first.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
            disabled={!page || busy}
            onClick={propose}
          >
            {busy ? 'Working…' : 'Propose selectors'}
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
            against {draft.page_url} · via {draft.provider}
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
                <span className="text-ink">{Object.values(spec.samples || {})[0] || '—'}</span>
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
            Present is not the same as proven — the darker slice is values carrying a verdict.
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

      <GapQueue datasetId={datasetId} />

      <BackfillPanel datasetId={datasetId} />

      <RefreshPanel datasetId={datasetId} />


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
          incumbent or adopting a rival both preserve evidence — there is no way
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
                Resolved — {outcome}.
              </Notice>
            ) : null}
            <div className="rounded-md border border-warn/40 bg-warn/5 p-2">
              <div className="text-xs uppercase tracking-wide text-muted">incumbent</div>
              <div className="truncate text-sm" title={String(c.incumbent.value ?? '')}>{truncate(String(c.incumbent.value ?? '-'), 220)}</div>
              {c.incumbent.quote ? (
                <div className="mt-1 border-l-2 border-rule-2 pl-2 text-xs text-muted">
                  “{c.incumbent.quote}”
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
                  {rv.url ? ` · ${host(rv.url)}` : ''}
                </div>
                <div className="truncate text-sm" title={String(rv.value ?? '')}>{truncate(String(rv.value ?? '-'), 220)}</div>
                {rv.quote ? (
                  <div className="mt-1 border-l-2 border-rule-2 pl-2 text-xs text-muted">
                    “{rv.quote}”
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

// Charts for an Ask result. The decision of what to draw is made from the
// data, not asked for: a column that parses as numbers gets magnitudes, a
// column with a handful of distinct values gets counts, and a `*__status`
// column gets a proportion bar. Anything else is left to the table, because a
// chart of 400 distinct strings is a picture of nothing.

const CHART_ROWS = 8;


function BarChart({ title, items, unit = '' }) {
  // One bar is not a chart. It is a sentence with axes, and drawing it implies
  // a distribution where there is only one value — which is what a model
  // writing `LIMIT 1` produces.
  if (!items || items.length < 2) return null;
  const max = Math.max(...items.map((i) => i.value)) || 1;
  return (
    <div className="min-w-0">
      <div className="mb-1.5 text-xs font-medium text-ink">{title}</div>
      <ul className="space-y-1">
        {items.map((i) => (
          <li key={i.label} className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-2">
            <span className="truncate text-[11px] text-muted" title={i.label}>
              {i.label}
            </span>
            <span className="h-2.5 w-full overflow-hidden rounded-full bg-warm">
              <span
                className="block h-full rounded-full"
                style={{
                  width: `${Math.max(1.5, (i.value / max) * 100)}%`,
                  background: i.tone ? toneVar(i.tone) : 'rgb(var(--accent))',
                }}
              />
            </span>
            <span className="tnum shrink-0 text-[11px] text-ink-2">
              {i.display ?? i.value}
              {unit}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ResultCharts({ columns, rows }) {
  if (!rows?.length || !columns?.length) return null;

  const numericCols = columns.filter((c) => numericShare(rows, c) >= 0.6).slice(0, 2);
  const catCols = columns.filter(
    (c) => !c.endsWith('__status') && !numericCols.includes(c) && topCategories(rows, c).length >= 2
  );
  const statusCols = columns.filter((c) => c.endsWith('__status')).slice(0, 1);

  const toneFor = (status) => (status === 'verified' ? 'ok'
    : status === 'conflicting' ? 'danger'
      : status === 'judgment_unavailable' ? 'judge' : 'warn');

  const charts = [];

  for (const col of numericCols) {
    const items = rows
      .map((r) => ({ label: String(r[col] ?? ''), value: Number(String(r[col]).replace(/[,$\s]/g, '')) }))
      .filter((i) => i.label && Number.isFinite(i.value))
      .sort((a, b) => b.value - a.value)
      .slice(0, CHART_ROWS)
      .map((i) => ({ ...i, display: num(i.value) }));
    charts.push(<BarChart key={`n:${col}`} title={`${col} — top values`} items={items} />);
  }

  for (const col of catCols.slice(0, 2 - charts.length)) {
    const items = topCategories(rows, col).map(([label, value]) => ({ label, value }));
    charts.push(<BarChart key={`c:${col}`} title={`${col} — by count`} items={items} />);
  }

  for (const col of statusCols) {
    const items = topCategories(rows, col, 6).map(([label, value]) => ({
      label: String(label || 'no verdict'),
      value,
      tone: toneFor(label),
    }));
    charts.push(
      <BarChart
        key={`s:${col}`}
        title={`${col.replace(/__status$/, '')} — how its values earned their keep`}
        items={items}
      />
    );
  }

  if (!charts.length) return null;
  return (
    <div className="space-y-2 rounded-lg border border-rule-2/60 p-2.5">
      <div className="text-xs uppercase tracking-wide text-muted">
        Charts — drawn from these {rows.length} row{rows.length === 1 ? '' : 's'}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">{charts}</div>
    </div>
  );
}

function ProposalCard({ datasetId, question, onExecuted }) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);

  async function approve() {
    setBusy(true);
    setFailure(null);
    try {
      await api.backfill(datasetId, { apply: true, limit_pages: 6 });
      onExecuted?.();
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  if (failure) return <ErrorNote error={failure} compact onRetry={approve} />;
  if (!question) return null;

  if (question.intent === 'query') {
    return <p className="text-xs text-muted">Asked as a question. No changes proposed.</p>;
  }

  // Only a backfill can be approved from here. A plan change goes to
  // /workflows/refine, which needs the plan's own id — and a dataset carries a
  // run id, not a plan id. Rendering one shared "Approve and fill" button for
  // both intents meant that asking for "only two sources instead of five" and
  // approving it ran a *backfill*: the wrong write, on a data path, from a
  // button that said something else. It is better to say where the change goes.
  const canFill = question.intent === 'backfill';

  return (
    <div className="space-y-2 rounded-lg border border-rule-2/60 p-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs uppercase tracking-wide text-muted">
          Proposal: {question.intent}
        </span>
        <span className="text-[11px] text-muted">
          confidence {question.confidence} · nothing has run
        </span>
      </div>
      <p className="text-xs">{question.summary}</p>
      {question.cost ? (
        <p className="text-[11px] text-muted">
          About {question.cost.extractions} extraction
          {question.cost.extractions === 1 ? '' : 's'} and up to{' '}
          {question.cost.judge_calls} verification calls, re-reading{' '}
          {question.cost.pages} stored page{question.cost.pages === 1 ? '' : 's'}.
          Nothing is crawled.
        </p>
      ) : null}
      {question.reason ? (
        <Notice tone="warn">{question.reason}</Notice>
      ) : !canFill ? (
        <Notice tone="info">
          A plan change is made where the plan is — start a run from this goal
          and adjust it there. Nothing here will touch the records.
        </Notice>
      ) : question.backfillable === false ? (
        <Notice tone="warn">This cannot be done safely on this dataset.</Notice>
      ) : (
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            className="rounded border border-ink px-2 py-1 text-xs disabled:opacity-40"
            onClick={approve}
          >
            {busy ? 'Running…' : 'Approve and fill'}
          </button>
          <span className="text-[11px] text-muted">
            Runs the verified path — every new value gets its own quote.
          </span>
        </div>
      )}
    </div>
  );
}

function AskPanel({ datasetId }) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [proposal, setProposal] = useState(null);
  const [failure, setFailure] = useState(null);

  async function ask(e) {
    e?.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setFailure(null);
    try {
      // Ask the SQL path and the proposal layer about the same sentence. The
      // proposal is read-only, so asking both costs one cheap request and
      // means a sentence that turns out to be a change never silently runs as
      // a query and never silently becomes a write.
      const text = question.trim();
      const p = await api.proposeChange(datasetId, { question: text });
      setProposal(p);
      if (p.intent === 'query') {
        setAnswer(await api.queryDataset(datasetId, { question: text, limit: 200 }));
      } else {
        setAnswer(null);
      }
    } catch (err) {
      setFailure(err);
      setAnswer(null);
      setProposal(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-medium">Ask this dataset</h3>
        <p className="text-xs text-muted">
          A question runs the guarded read-only SQL path. A request to change
          something becomes a proposal you approve first — the model never
          writes to a dataset on its own.
        </p>
      </div>

      <form onSubmit={ask} className="flex gap-2">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. how many companies are in each industry?"
          aria-label="Question for this dataset"
          className="min-w-0 flex-1 rounded border border-rule-2 bg-transparent px-2 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={busy || !question.trim()}
          className="shrink-0 rounded border border-rule-2 px-3 py-1.5 text-sm disabled:opacity-40"
        >
          {busy ? 'Working…' : 'Ask'}
        </button>
      </form>

      {failure ? <ErrorNote error={failure} onRetry={ask} /> : null}
      {proposal ? (
        <ProposalCard
          datasetId={datasetId}
          question={proposal}
          onExecuted={() => {
            setProposal(null);
            window.location.reload();
          }}
        />
      ) : null}

      {answer ? (
        <div className="space-y-2">
          <div className="rounded-md border border-rule-2/60 bg-warm/40 p-2">
            <div className="text-xs uppercase tracking-wide text-muted">SQL</div>
            <code className="mt-1 block overflow-x-auto text-xs">{answer.sql}</code>
          </div>
          <div className="text-xs text-muted">
            {answer.row_count} row{answer.row_count === 1 ? '' : 's'}
            {answer.truncated ? ' (capped)' : ''}
          </div>
          <ResultCharts columns={answer.columns} rows={answer.rows} />
          {answer.rows?.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    {answer.columns.map((c) => (
                      <th key={c} className="border-b border-rule-2/60 px-2 py-1 text-left text-xs font-medium">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {answer.rows.map((row, i) => (
                    <tr key={i} className="border-b border-rule-2/30 last:border-0">
                    {answer.columns.map((c) => (
                      <td key={c} className="max-w-[280px] px-2 py-1 align-top">
                        {/* A block-level truncating span: `max-w` on a <td> is
                            only a hint in an auto-layout table, so a long
                            description stretched the whole grid instead of
                            clipping. */}
                        <span className="block max-w-[260px] truncate" title={String(row[c] ?? '')}>
                          {row[c] === null || row[c] === undefined ? (
                            <span className="text-muted">—</span>
                          ) : (
                            String(row[c])
                          )}
                        </span>
                      </td>
                    ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="No rows matched" hint="The query ran; nothing satisfied it." />
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Re-read the sources this dataset already used.
 *
 * The counterpart to Backfill, and kept visibly separate because it is the only
 * operation here that can replace a value which already exists. The three rules
 * that make that safe are stated on the button rather than in a tooltip: the
 * sources are re-fetched rather than reused, a replacement must carry its own
 * verified quote, and the value it replaces is kept as a rival so the change
 * stays reviewable instead of becoming a silent correction.
 */
function RefreshPanel({ datasetId }) {
  const [proposal, setProposal] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [named, setNamed] = useState('');

  const load = useCallback(
    (q) => api.refreshProposal(datasetId, q ? { sources: q, limit: 6 } : { limit: 6 }),
    [datasetId]
  );

  useEffect(() => {
    let alive = true;
    load('')
      .then((p) => { if (alive) { setProposal(p); setLoaded(true); } })
      .catch((e) => { if (alive) { setFailure(e); setLoaded(true); } });
    return () => { alive = false; };
  }, [load]);

  async function run(apply) {
    setBusy(true);
    setFailure(null);
    const q = named.trim();
    try {
      setResult(await api.refresh(datasetId, {
        apply,
        limit: 6,
        sources: q ? q.split(',').map((s) => s.trim()).filter(Boolean) : [],
      }));
      if (apply) setProposal(await load(q));
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;
  const p = proposal || {};

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium">Refresh sources</h3>
          <p className="text-xs text-muted">
            {p.refreshable ? (
              <>
                Re-fetches {p.sources} source{p.sources === 1 ? '' : 's'}
                {p.hosts?.length ? <> ({p.hosts.slice(0, 4).join(', ')})</> : null} and
                re-verifies the {p.cells_reverified} value
                {p.cells_reverified === 1 ? '' : 's'} they supplied. A value is
                replaced only if the new one carries its own quote, and the value
                it replaces is kept for review.
              </>
            ) : (
              p.reason || 'No source recorded for this dataset yet.'
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={named}
            onChange={(e) => setNamed(e.target.value)}
            placeholder="host, host… (blank = best yield)"
            aria-label="Hosts to refresh"
            className="w-44 rounded border border-rule-2 bg-transparent px-2 py-1 text-xs"
          />
          <button
            type="button"
            disabled={busy || !p.refreshable}
            className="rounded border border-rule-2 px-2 py-1 text-xs disabled:opacity-40"
            onClick={() => { setResult(null); load(named.trim()).then(setProposal).catch(setFailure); }}
          >
            Check
          </button>
          <button
            type="button"
            disabled={busy || !p.refreshable}
            className="rounded border border-ink px-2 py-1 text-xs disabled:opacity-40"
            onClick={() => run(false)}
          >
            {busy ? 'Re-fetching…' : 'Dry run'}
          </button>
          <button
            type="button"
            disabled={busy || !p.refreshable}
            className="rounded border border-ink px-2 py-1 text-xs disabled:opacity-40"
            onClick={() => run(true)}
          >
            Apply
          </button>
        </div>
      </div>

      {failure ? <ErrorNote error={failure} compact onRetry={() => run(false)} /> : null}

      {result ? (
        <div className="space-y-1 rounded-lg border border-rule-2/60 p-2 text-xs">
          <div>
            Re-fetched {result.refetched} source{result.refetched === 1 ? '' : 's'},
            rechecked {result.records_rechecked} record
            {result.records_rechecked === 1 ? '' : 's'}:{' '}
            <strong>{result.changed}</strong> value{result.changed === 1 ? '' : 's'} differ,
            {' '}
            <strong>{result.unchanged}</strong> unchanged,{' '}
            <strong>{result.unverifiable}</strong> not provable, {result.unmatched} unmatched.
            {result.applied ? (
              <>
                {' '}
                <strong>{result.written}</strong> written.
              </>
            ) : (
              ' Nothing was written — this was a dry run that really re-fetched.'
            )}
          </div>
          {result.changed_examples?.length ? (
            <ul className="ml-3 list-disc space-y-0.5 text-muted">
              {result.changed_examples.map((c, i) => (
                <li key={i}>
                  <span className="font-mono">{c.field}</span>: {String(c.old)} →{' '}
                  {String(c.new)}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SourceGrid({ datasetId }) {
  const { data, status, error, refetch } = useResource(
    (o) => api.sources(datasetId, o),
    [datasetId]
  );
  // Yield is a separate read because it derives from the records, not the
  // sources: "how many verified cells did this host actually produce" is a
  // question about quotes. Failing to load it must not blank the source list,
  // so it is a second resource rather than a nested one.
  const { data: yieldData } = useResource((o) => api.yieldMap(datasetId, o), [datasetId]);
  const { inspect } = useInspector();

  // Returns `{dataset_id, sources_attempted, sources_successful, sources_failed, sources[]}`.
  const sources = useMemo(() => data?.sources || [], [data]);
  const attempted = data?.sources_attempted ?? null;
  const successful = data?.sources_successful ?? null;
  const failed = data?.sources_failed ?? null;
  const hosts = useMemo(() => yieldData?.hosts || {}, [yieldData]);
  const yieldFor = (h) => hosts[h] || null;

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
  const reusedCount = sources.filter((s) => s.status === 'reused').length;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Pages stored"
          value={num(sources.length)}
          sub={attempted !== null ? `${num(attempted)} attempted` : undefined}
        />
        <Stat
          label="Reused, not re-fetched"
          value={num(reusedCount)}
          tone={reusedCount ? 'info' : 'muted'}
          sub={
            reusedCount
              ? 'stored evidence, shown with its real age'
              : 'everything was fetched for this run'
          }
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
          const y = yieldFor(h);
          return (
            <section key={h} className="surface overflow-hidden">
              <header className="flex items-center justify-between gap-2 border-b border-rule px-3.5 py-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Dot tone={ok === list.length ? 'ok' : ok ? 'warn' : 'danger'} />
                  <span className="truncate font-mono text-[12px] text-ink">{h}</span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {/* What this host earned, not what it cost. Read from the
                      quotes on the cells it supplied, so a host with pages but
                      no verified cell reads as zero rather than as absent. */}
                  {y && y.pages > 0 && (
                    <span
                      className="text-[10.5px] text-muted"
                      title={`${num(y.verified)} proven cell(s) from ${num(
                        y.records
                      )} record(s), across ${num(y.pages)} stored page(s). ${
                        y.yield
                      } proven per page.`}
                    >
                      {num(y.verified)} proven · {y.yield}/page
                    </span>
                  )}
                  <span className="text-[11px] text-muted">
                    {list.length} page{list.length === 1 ? '' : 's'}
                  </span>
                </div>
              </header>
              <ul className="divide-y divide-rule">
                {list.map((s, i) => {
                  const url = s.url || s.page_url;
                  const good = s.status === 'ok';
                  // A reused source is a page this machine already had. It is
                  // not `ok`-shaped in the same way: nothing was requested, and
                  // the evidence may predate the page changing upstream. It
                  // gets its own marker and its real retrieval date, because
                  // presenting it as a fresh fetch would be a quiet lie.
                  const wasReused = s.status === 'reused';
                  return (
                    <li key={`${s.content_hash || url}-${i}`}>
                      <div className="row flex items-center gap-2.5 px-3.5 py-2">
                        <span
                          className={`shrink-0 ${good ? 'text-ok' : 'text-danger'}`}
                          title={good ? 'Fetched and stored' : s.error || 'Failed'}
                          aria-label={good ? 'Fetched' : 'Failed'}
                        >
                          {good ? '✓' : '✗'}
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
                          {wasReused ? (
                            <span
                              className="shrink-0 text-[10px] text-info"
                              title={
                                s.retrieved_at
                                  ? `Not re-fetched. This is the copy stored on ${new Date(
                                      s.retrieved_at
                                    ).toLocaleString('en-US')}. It may predate a change to the page.`
                                  : 'Not re-fetched; reused from an earlier run.'
                              }
                            >
                              reused{s.retrieved_at ? ` · ${when(s.retrieved_at)}` : ''}
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
  /*
   * The field profiler is read here rather than inside the Fields tab, because
   * the domain reading is a property of the *dataset* and not of a tab: it was
   * buried under Fields, and the page opens on Records, so opening a dataset
   * never showed it and the feature read as broken. One fetch, passed down, so
   * the two views cannot show different numbers for the same fields.
   */
  const profileRes = useResource((o) => api.profile(id, o), [id]);

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

      <SectorPanel
        profile={profileRes.data}
        status={profileRes.status}
        error={profileRes.error}
        onRetry={profileRes.refetch}
      />

      <Segmented
        label="Dataset section"
        value={tab}
        onChange={setTab}
        options={[
          { value: 'fields', label: 'Fields' },
          { value: 'records', label: 'Records' },
          { value: 'review', label: 'Review' },
          { value: 'coverage', label: 'Gaps' },
          { value: 'conflicts', label: 'Conflicts' },
          { value: 'sources', label: 'Sources' },
          { value: 'ask', label: 'Ask' },
          { value: 'export', label: 'Export' },
        ]}
      />

      {tab === 'fields' ? (
        <FieldsView datasetId={id} profile={profileRes.data} />
      ) : null}

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
          onCell={(record, field, index) =>
            inspect({
              kind: 'record',
              record,
              // The rail opens on this field, with the stored page already
              // resolved when offsets allow it — the reader clicked a value
              // because they want the sentence behind it, not a summary of it.
              focusField: field,
              title: `${field}`,
              sub: `Record ${index + 1} · ${data.name}`,
              json: record.fields?.[field],
            })
          }
        />
      ) : null}

      {tab === 'review' ? (
        <ReviewQueue
          datasetId={id}
          schema={data.schema}
          onOpenConflicts={() => setTab('conflicts')}
        />
      ) : null}

      {tab === 'coverage' ? <CoveragePanel datasetId={id} runId={data.run_id} /> : null}
      {tab === 'conflicts' ? <ConflictsPanel datasetId={id} /> : null}
      {tab === 'sources' ? <SourceGrid datasetId={id} /> : null}
      {tab === 'ask' ? <AskPanel datasetId={id} /> : null}
      {tab === 'export' ? <ExportPanel datasetId={id} schema={data.schema} /> : null}
    </div>
  );
}

const pctSafe = (n) => (Number.isFinite(n) ? `${Math.round(n * 100)}%` : '—');
