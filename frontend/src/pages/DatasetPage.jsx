import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, downloadBlob } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import FieldsView from '../components/FieldsView.jsx';
import GapQueue from '../components/GapQueue.jsx';
import SectorPanel from '../components/SectorPanel.jsx';
import ReviewQueue from '../components/ReviewQueue.jsx';
import DatasetRail from '../components/dataset/DatasetRail.jsx';
import DatasetHeader from '../components/dataset/DatasetHeader.jsx';
import DatasetHealth from '../components/dataset/DatasetHealth.jsx';
import DatasetOverview from '../components/dataset/DatasetOverview.jsx';
import RecordsTable from '../components/dataset/RecordsTable.jsx';
import { datasetViewModel } from '../lib/datasetViewModel.js';
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
  Spinner,
  StackedBar,
  Stat,
} from '../components/ui.jsx';
import {
  bytes,
  host,
  num,
  toneVar,
  truncate,
  when,
} from '../lib/format.js';

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
            : 'Nothing to backfill — every declared field already has a value on at least one record.'}{' '}
          A filled cell is never overwritten, in either mode: backfill only writes cells that are empty.
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
      ) : null}
      {/*
        Unconditional, and outside the branch above on purpose. Whether or not
        this proposal is fillable, the backfill path only writes cells that are
        empty — that is the guarantee the panel exists to state, and gating it
        behind `backfillable` hid it on exactly the datasets whose readers most
        needed to see it.
      */}
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
  const { data: pages } = useResource((o) => api.runPages(runId, undefined, o), [runId]);
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
        // `overwrite` belongs in the *body*, not in the options slot. It was
        // passed as the second argument, where it was spread into the fetch
        // options object and silently discarded — so the checkbox was decorative
        // and the server always took its own default.
        {
          domain: draft.domain,
          page_url: draft.page_url,
          provider: draft.provider,
          fields: draft.fields,
          overwrite: Boolean(overwrite),
        }
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
  // Decided cards come first, not last. They are the reader's own just-finished
  // work, and the confirmation ("Resolved — adopted rival 1") is the thing the
  // eye needs next. Appending them after the live list pushed a decision to
  // index 63 on a 64-conflict dataset, past the 40-card render window — so the
  // write succeeded, the server agreed, and the panel showed nothing.
  const rows = [...Object.values(resolved).map((r) => r.conflict), ...live];
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

// Bars for an agent answer. The agent's chart spec is already vetted
// server-side — same labels, same counts as a distribution a tool returned —
// so this only draws. One bar is still not a chart: it is a sentence with
// axes, and drawing it implies a distribution where there is only one value.
function BarChart({ title, items, unit = '' }) {
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

/**
 * Ask this dataset — answered with tools, not with SQL.
 *
 * The question goes to one endpoint that runs a bounded agent loop: the model
 * picks read tools from a fixed menu, the server executes them against stored
 * records, and the model composes the answer from the results. No
 * model-authored string ever touches the data, and nothing in this path
 * writes — a request to change something comes back as a proposal that the
 * reader approves through the same buttons the Gaps and Conflicts sections
 * already offer.
 *
 * What the panel shows, in order: the answer in words, the figures it cites,
 * the distribution as bars when one answers the question better, and then how
 * it found out — the tool trace, one line per call. The trace is not
 * decoration: it is what lets a reader check that the answer came from the
 * dataset rather than from the model's prior about what such datasets
 * usually contain.
 */
const ASK_SUGGESTIONS = [
  'Which fields are the thinnest, and why?',
  'Where is the evidence weakest?',
  'What do the sources disagree about?',
  'Which hosts supplied the most proven values?',
];

function AskPanel({ datasetId }) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [failure, setFailure] = useState(null);

  async function ask(e, text) {
    e?.preventDefault();
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      setResult(await api.askAgent(datasetId, { question: q }));
    } catch (err) {
      setFailure(err);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const proposals = result?.proposals || [];

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-medium">Ask this dataset</h3>
        <p className="text-xs text-muted">
          Questions are answered with read tools over the stored records —
          distributions, coverage, conflicts, gaps and sources. The model picks
          the tools and composes the answer from what they return; it cannot
          write to the dataset. A request to change something becomes a proposal
          you approve first.
        </p>
      </div>

      <form onSubmit={ask} className="flex gap-2">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. which values appear most often?"
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

      <div className="flex flex-wrap gap-1.5">
        {ASK_SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            disabled={busy}
            onClick={() => {
              setQuestion(s);
              ask(null, s);
            }}
            className="rounded-full border border-rule-2 px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-ink/40 hover:text-ink disabled:opacity-40"
          >
            {s}
          </button>
        ))}
      </div>

      {failure ? <ErrorNote error={failure} onRetry={(e) => ask(e)} /> : null}

      {proposals.map((p, i) => (
        <ProposalCard
          key={i}
          datasetId={datasetId}
          question={p}
          onExecuted={() => {
            setResult(null);
            window.location.reload();
          }}
        />
      ))}

      {result && result.intent === 'query' ? (
        <div className="space-y-3">
          <div className="surface p-3">
            <p className="eyebrow">Answer</p>
            <p className="mt-1.5 text-[13.5px] leading-relaxed text-ink">{result.answer}</p>
            {(result.figures || []).length ? (
              <dl className="mt-2.5 space-y-1 border-t border-rule pt-2">
                {result.figures.map((f, i) => (
                  <div key={i} className="flex items-baseline justify-between gap-3">
                    <dt className="min-w-0 truncate text-[11.5px] text-muted">{f.label}</dt>
                    <dd className="shrink-0 font-mono text-[12px] tnum text-ink">{String(f.value)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>

          {result.chart?.items?.length ? (
            <div className="surface p-3">
              <BarChart
                title={result.chart.title || 'Distribution'}
                items={result.chart.items.map((it) => ({
                  label: String(it.label ?? ''),
                  value: Number(it.n) || 0,
                  display: num(Number(it.n) || 0),
                }))}
              />
            </div>
          ) : null}

          {(result.trace || []).length ? (
            <details className="surface px-3 py-2">
              <summary className="focusable cursor-pointer list-none py-1 text-[11.5px] text-muted transition-colors hover:text-ink">
                How it found out — {result.trace.length} tool
                {result.trace.length === 1 ? '' : 's'} over the stored records
              </summary>
              <ol className="space-y-1.5 border-t border-rule py-2.5">
                {result.trace.map((t, i) => (
                  <li key={i} className="grid grid-cols-[minmax(0,9rem)_1fr] items-baseline gap-2 text-[11.5px]">
                    <code className="truncate font-mono text-ink-2" title={t.tool}>
                      {t.tool}
                    </code>
                    <span className="text-muted">{t.summary}</span>
                  </li>
                ))}
              </ol>
            </details>
          ) : null}

          {result.provider || result.model ? (
            <p className="text-[10.5px] text-muted">
              Answered with read tools
              {result.model ? (
                <>
                  {' '}by <span className="font-mono">{result.model}</span>
                </>
              ) : null}
              {result.facts?.records !== undefined ? (
                <>
                  {' '}over <span className="font-mono tnum">{num(result.facts.records)}</span> stored records
                </>
              ) : null}
              .
            </p>
          ) : null}
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
      // One shape for every format: `api.exportDataset` builds the blob from the
      // envelope's `content`. It used to differ per format on the belief that
      // csv/md were raw attachments, so those downloads saved the envelope JSON
      // under a `.csv` name.
      downloadBlob(res.blob, res.filename);
      setDone(res.format.toUpperCase());
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
        recorded rather than free. All three formats return the rendered file in the same
        response; the format only changes what that file contains.
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

/**
 * The dataset workspace.
 *
 * The composition here is the whole redesign; the panels it switches between are
 * the ones that were already in this file, unchanged.
 *
 * WHAT CHANGED, AND WHY IT IS NOT A REWRITE
 *
 * The eight sections were a horizontal tab strip at the bottom of a page whose
 * entire first screen was summary cards. Opening a dataset meant scrolling past
 * record count, proven-field count and schema size before seeing a single row,
 * and the way into the data was a strip of eight equal-looking words.
 *
 * Now the dataset's identity and its four health signals stay on screen, the
 * sections are a vertical rail, and the default section is the overview — which
 * leads with the ledger rather than with more metadata. Every API call, route,
 * record shape and action below is the one that was already here.
 *
 * WHY THE MODEL IS BUILT HERE
 *
 * Four reads are needed for the health signals and the findings, and three of
 * them are only used by the overview. They are issued unconditionally rather
 * than per-section so switching sections never re-fetches, and they are all
 * optional arguments to `datasetViewModel`, which degrades section by section —
 * which is what lets the header, the health strip and the ledger all paint
 * before the slowest read lands.
 *
 * WHY THE SECTION IS NOT IN THE URL
 *
 * It could be, and that would make a dataset section linkable. It is left as
 * local state for now because `/library/:id` is the only route contract the rest
 * of the app, the command palette and the end-to-end suites rely on, and adding
 * a query parameter to that route is a change to a contract shared well beyond
 * this page's presentation. It is the obvious next step and it is a deliberate
 * omission, not an oversight.
 */
export default function DatasetPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { inspect } = useInspector();

  /*
   * Overview leads. Records is second, which is also the order the end-to-end
   * suites assume when they select a tab by index rather than by name.
   */
  const [tab, setTab] = useState('overview');

  const { data, status, error, refetch } = useResource((o) => api.dataset(id, o), [id]);

  /*
   * The field profiler is read here rather than inside the Fields section,
   * because the domain reading and the data shapes are properties of the
   * *dataset* and not of a section — they belong on the overview. One fetch,
   * passed down, so the overview and the field list cannot show different
   * numbers for the same fields.
   */
  const profileRes = useResource((o) => api.profile(id, o), [id]);

  /* Coverage and gaps drive three of the four health signals and several of the
     findings, so they are read once for the whole page. */
  const coverageRes = useResource((o) => api.coverage(id, o), [id]);
  const gapsRes = useResource((o) => api.gaps(id, o), [id]);

  /*
   * Freshness is the one signal no existing counter carries, so the sources read
   * is needed at page level too. It is the same payload the Sources section
   * reads again for its own detail, and `useResource` keeps it cached per
   * component rather than shared, so this is one extra small request in exchange
   * for a header that can state how old the evidence actually is.
   */
  const sourcesRes = useResource((o) => api.sources(id, o), [id]);

  const model = useMemo(
    () =>
      datasetViewModel({
        dataset: data,
        profile: profileRes.data,
        coverage: coverageRes.data,
        gaps: gapsRes.data,
        sources: sourcesRes.data,
      }),
    [data, profileRes.data, coverageRes.data, gapsRes.data, sourcesRes.data]
  );

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

  const openRecord = (record, index) =>
    inspect({
      kind: 'record',
      record,
      title: `Record ${index + 1}`,
      sub: data.name,
      json: record,
    });

  const openCell = (record, field, index) =>
    inspect({
      kind: 'record',
      record,
      // The inspector opens on this field, with the stored page already resolved
      // when the offsets allow it — the reader clicked a value because they want
      // the sentence behind it, not a summary of it.
      focusField: field,
      title: `${field}`,
      sub: `Record ${index + 1} · ${data.name}`,
      json: record.fields?.[field],
    });

  /*
   * Rail counts. A null count renders nothing rather than a zero — "0
   * conflicts" and "conflicts not yet read" are different facts, and the rail is
   * the one place a reader would take the second for the first.
   */
  const railCounts = {
    records: model.summary.records,
    fields: model.identity.fieldCount,
    coverage: model.summary.gaps,
    conflicts: model.summary.openConflicts,
    sources: model.summary.pages,
  };

  return (
    <div className="space-y-5">
      <DatasetHeader
        identity={model.identity}
        model={model}
        onInspectRaw={() => inspect({ kind: 'event', event: data, title: 'Raw dataset' })}
        onDeleted={() => navigate('/library')}
      />

      {/* A hairline rather than a card. This block used to be four stat cards
          and a bordered panel, and the borders were most of what made it read
          as a dashboard rather than as a document. */}
      <div className="border-y border-rule py-3">
        <DatasetHealth health={model.health} onOpen={setTab} />
      </div>

      <div className="flex flex-col gap-4 sm:flex-row sm:gap-6">
        {/*
          One rail at every width. It reflows from a vertical column to a
          horizontal scroller on its own, so the document never holds two
          tablists with the same accessible name.
        */}
        <div className="sm:sticky sm:top-4 sm:self-start">
          <DatasetRail value={tab} onChange={setTab} counts={railCounts} />
        </div>

        <div className="min-w-0 flex-1">

          {tab === 'overview' ? (
            <DatasetOverview
              model={model}
              datasetId={id}
              schema={data.schema}
              onOpen={setTab}
              onPick={openRecord}
              onCell={openCell}
            />
          ) : null}

          {tab === 'records' ? (
            <RecordsTable
              datasetId={id}
              schema={data.schema}
              onPick={openRecord}
              onCell={openCell}
            />
          ) : null}

          {tab === 'fields' ? (
            <div className="space-y-6">
              {/* The domain reading and its receipt, above the full field list.
                  The sector is a claim about which fields matter, and the
                  charts beneath it are the argument for it. */}
              <SectorPanel
                profile={profileRes.data}
                status={profileRes.status}
                error={profileRes.error}
                onRetry={profileRes.refetch}
              />
              <FieldsView datasetId={id} profile={profileRes.data} />
            </div>
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
      </div>
    </div>
  );
}