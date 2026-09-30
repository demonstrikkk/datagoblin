import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import { ErrorNote, Loading, Notice } from './ui.jsx';
import {
  CellStatus,
  EvidenceQuote,
  GROUPED_STATUS,
  VoiceEmpty,
  asField,
  canProve,
} from './evidence.jsx';
import { REC_VERIFY, host, num, when } from '../lib/format.js';

/** How many records to read per page while assembling the queue. */
const PAGE = 200;

/**
 * How many pages to read.
 *
 * A review queue that silently reviewed the first thousand values and called
 * itself complete would be worse than one that admits a limit, so the ceiling
 * is a number the header can print. It is generous because a dataset of any
 * real size exceeds it, and the header says so when it does.
 */
const MAX_PAGES = 5;

/**
 * Statuses worth a human's attention.
 *
 * `verified` is excluded — those are finished. `rejected` is excluded too: the
 * validator already dropped those values, so there is nothing left to decide
 * and listing them would inflate the count with work that is done.
 */
const ATTENTION = new Set([
  'unverified',
  'conflicting',
  'judgment_unavailable',
  'rate_limited',
  'needs_review',
]);

/**
 * How urgent a value is to a human, highest first.
 *
 * The first cut of this queue was ordered by storage, which put a page of
 * empty cells with no quote at the top: technically they all needed attention,
 * and practically there was nothing anyone could do with any of them. A review
 * queue that opens on six identical "no evidence at all" rows reads as noise and
 * trains the reader to skip it.
 *
 * So the order is by what a person can actually do:
 *
 *   0  another source disagrees        resolve it — the API supports it
 *   1  a quote and a stored page exist read it and judge it
 *   2  a quote exists, no stored page  the quote is all there is
 *   3  a source, no quote              nothing to check yet
 *   4  nothing recorded at all         informational only
 */
function priority(it) {
  const s = it.prov.source;
  if (it.prov.status === 'conflicting') return 0;
  if (s?.quote && s?.page_id) return 1;
  if (s?.quote) return 2;
  if (s) return 3;
  return 4;
}

const PRIORITY_NOTE = [
  'another source disagrees',
  'a quote and a stored page are available',
  'a quote was captured but no page was stored',
  'a source was recorded but no quote was captured',
  'nothing was recorded for this value',
];

/**
 * The review queue.
 *
 * Everything the pipeline could not settle on its own, in one list, each entry
 * showing the value and the evidence behind it side by side. This is the
 * difference between a scraper and a verification workstation: the values are
 * not quietly rounded to "unverified" and shipped, they are held still and put
 * in front of a person.
 *
 * Deliberately has no Accept / Reject / Keep-empty buttons.
 *
 * The design brief sketched those, and it would be the obvious thing to build —
 * but the API has no endpoint that would accept them. `/conflicts/resolve`
 * settles a conflict between two *recorded* rivals, and that is the only write
 * path on a dataset. A queue whose buttons either did nothing or invented an
 * endpoint would be the single most misleading thing in the product: a row of
 * controls that look like adjudication and are not. So the queue is honest
 * about what it can do, which is show the evidence and let a person decide
 * outside the tool. Conflicting values are pointed at the Conflicts section,
 * where resolution actually exists.
 */
export default function ReviewQueue({ datasetId, schema, onOpenConflicts }) {
  const { inspect } = useInspector();
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState('all');

  const { data, loading, error, refetch } = useResource(
    (o) => api.records(datasetId, { limit: PAGE, offset: page * PAGE }, o),
    [datasetId, page]
  );

  const records = data?.records || [];
  const total = data?.total ?? 0;
  const readAll = records.length >= total || (page + 1) * PAGE >= Math.min(total, MAX_PAGES * PAGE);

  /* Flatten to one entry per value that needs a decision, most actionable
     first. Sorting here rather than after the filter keeps the rank stable
     while the reader narrows by reason. */
  const items = useMemo(() => {
    const out = [];
    records.forEach((r, ri) => {
      const fields = r?.fields && typeof r.fields === 'object' ? r.fields : {};
      Object.entries(fields).forEach(([name, prov]) => {
        const pf = asField(prov);
        if (!ATTENTION.has(pf.status)) return;
        out.push({
          key: `${page}-${ri}-${name}`,
          name,
          prov: pf,
          record: r,
          // The row's ordinal within the whole dataset, so the reader can find
          // the same row in the Records table.
          ordinal: page * PAGE + ri + 1,
        });
      });
    });
    return out.sort((a, b) => priority(a) - priority(b));
  }, [records, page]);

  const shown = useMemo(
    () => (status === 'all' ? items : items.filter((it) => it.prov.status === status)),
    [items, status]
  );

  const counts = useMemo(() => {
    const c = new Map();
    items.forEach((it) => c.set(it.prov.status, (c.get(it.prov.status) || 0) + 1));
    return c;
  }, [items]);

  /**
   * How much of the queue is actually reviewable, stated plainly.
   *
   * "362 values need a decision" invites the reader to start at the top and
   * work down, so the number has to say what kind of work it is. Most
   * unresolved values have no quote to read at all, and without this split the
   * headline count implies far more adjudicable work than exists.
   */
  const bands = useMemo(() => {
    const b = [0, 0, 0, 0, 0];
    items.forEach((it) => {
      b[priority(it)] += 1;
    });
    return b;
  }, [items]);

  const reviewable = bands[0] + bands[1] + bands[2];
  const checkable = bands[0] + bands[1];

  /* Open the evidence rail on one value, exactly as clicking its cell would. */
  const inspectItem = (it) =>
    inspect({
      kind: 'record',
      record: it.record,
      focusField: it.name,
      title: it.name,
      sub: `Record ${it.ordinal}`,
      json: it.record.fields?.[it.name],
    });

  if (error) {
    return (
      <div className="space-y-3">
        <ErrorNote error={error} onRetry={refetch} />
      </div>
    );
  }

  if (loading && !items.length) return <Loading rows={5} label="Building the review queue" />;

  if (!items.length && !readAll) {
    /* Only page one was read and it held nothing worth reviewing. */
    return (
      <Notice tone="muted">
        Nothing on the first {num(records.length || PAGE)} records needs a decision.{' '}
        <button type="button" className="link" onClick={() => setPage((p) => p + 1)}>
          Read the next {num(PAGE)}
        </button>
      </Notice>
    );
  }

  if (!items.length) {
    return (
      <VoiceEmpty
        state="noConflicts"
        action={
          readAll && total > records.length ? (
            <button type="button" className="btn-outline btn-xs" onClick={() => setPage(0)}>
              Start again
            </button>
          ) : null
        }
      />
    );
  }

  return (
    <div className="space-y-3">
      {/* The count is the point of this section, so it is stated as a sentence
          with its own limit, not hidden in a tab badge. */}
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <p className="text-[13px] text-ink-2">
            <span className="font-mono text-[15px] text-ink">{num(items.length)}</span>{' '}
            {items.length === 1 ? 'value needs' : 'values need'} a decision
            {!readAll && total > records.length ? (
              <span className="text-muted">
                {' '}
                — read so far from {num(records.length)} of {num(total)} records
              </span>
            ) : null}
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            <select
              className="input-sm input w-auto"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              aria-label="Filter the review queue by status"
            >
              <option value="all">Every reason</option>
              {[...counts.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([k, n]) => (
                  <option key={k} value={k}>
                    {/* The backend's own label, not the grouped display word.
                        `unverified` and `judgment_unavailable` both group to
                        "Unresolved", and a filter that offers "Unresolved"
                        twice with different counts cannot be told apart — but
                        they are different situations, and the difference is
                        exactly what someone filtering this queue needs. */}
                    {REC_VERIFY[k]?.label || k} ({n})
                  </option>
                ))}
            </select>
            {counts.has('conflicting') && onOpenConflicts ? (
              <button type="button" className="btn-outline btn-xs" onClick={onOpenConflicts}>
                {num(counts.get('conflicting'))} can be resolved
              </button>
            ) : null}
          </div>
        </div>

        {/* The breakdown. Without it the headline count overstates how much of
            the work a person can actually do, because most unresolved values
            have no quote to read in the first place. */}
        <ul className="flex flex-wrap gap-x-4 gap-y-1">
          {bands.map((n, i) =>
            n ? (
              <li key={i} className="flex items-baseline gap-1.5 text-[11px] text-muted">
                <span className="font-mono tnum text-ink-2">{num(n)}</span>
                <span>{PRIORITY_NOTE[i]}</span>
              </li>
            ) : null
          )}
        </ul>
        {items.length > reviewable ? (
          <p className="text-[11px] text-warn">
            {num(checkable)} can be checked against stored evidence and{' '}
            {num(bands[0])} resolved in place; the other {num(items.length - reviewable)}{' '}
            {items.length - reviewable === 1 ? 'has' : 'have'} no quote to read, so there is
            nothing to decide until a run captures one.
          </p>
        ) : null}
      </div>

      <ol className="surface divide-y divide-rule overflow-hidden">
        {shown.map((it, i) => {
          const meta = REC_VERIFY[it.prov.status] || null;
          const src = it.prov.source;
          return (
            <li key={it.key} className="row">
              <div className="grid gap-x-4 gap-y-2 px-3.5 py-3 sm:grid-cols-[1fr_auto]">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="eyebrow">{it.name}</span>
                    <span className="text-[10.5px] text-muted">
                      record {num(it.ordinal)}
                    </span>
                    {meta ? (
                      <span className="inline-flex items-center gap-1 text-[10.5px] text-muted">
                        <CellStatus status={it.prov.status} size="sm" />
                        {GROUPED_STATUS[it.prov.status] || meta.label}
                      </span>
                    ) : null}
                  </div>

                  <p className="mt-1 break-words text-[13px] leading-snug text-ink">
                    {it.prov.value === null || it.prov.value === undefined || it.prov.value === '' ? (
                      <span className="text-rule-2">—</span>
                    ) : typeof it.prov.value === 'object' ? (
                      JSON.stringify(it.prov.value)
                    ) : (
                      String(it.prov.value)
                    )}
                  </p>

                  <div className="mt-2 border-l border-rule-2 pl-2.5">
                    <EvidenceQuote value={it.prov.value} quote={src?.quote} />
                  </div>

                  <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-muted">
                    {src?.url ? (
                      <a
                        href={src.url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="link font-mono"
                      >
                        {host(src.url)} ↗
                      </a>
                    ) : (
                      <span className="text-warn">no source recorded</span>
                    )}
                    {src?.page_id ? (
                      <span className="font-mono">page {String(src.page_id).slice(0, 8)}</span>
                    ) : null}
                    {src?.retrieved_at ? <span>{when(src.retrieved_at)}</span> : null}
                    {canProve(src) ? (
                      <span className="text-ok">resolves in the stored page</span>
                    ) : null}
                  </div>
                </div>

                <div className="flex items-start gap-1.5 sm:flex-col sm:items-end">
                  <button
                    type="button"
                    className="btn-outline btn-xs whitespace-nowrap"
                    onClick={() => inspectItem(it)}
                  >
                    Inspect
                  </button>
                  <span className="font-mono text-[10px] text-rule-2 tnum">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                </div>
              </div>
            </li>
          );
        })}
      </ol>

      {meta_note(readAll, total, records.length, page, setPage)}
    </div>
  );
}

/** Paging and honesty about what has and has not been read. */
function meta_note(readAll, total, read, page, setPage) {
  if (readAll) {
    if (read >= total) {
      return (
        <p className="text-[11px] text-muted">
          Every one of the {num(total)} records was read.
        </p>
      );
    }
    return (
      <p className="text-[11px] text-warn">
        Read the first {num(read)} of {num(total)} records. Raise the page limit to review the
        rest — the endpoint caps a single read at {num(PAGE * MAX_PAGES)}.
      </p>
    );
  }
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[11px] text-muted">
        Read {num(read)} of {num(total)} records
      </span>
      <button
        type="button"
        className="btn-outline btn-xs"
        onClick={() => setPage((p) => p + 1)}
        disabled={(page + 1) * PAGE >= Math.min(total, MAX_PAGES * PAGE)}
      >
        Read more
      </button>
    </div>
  );
}
