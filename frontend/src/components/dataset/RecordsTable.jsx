import { useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api.js';
import useResource from '../../hooks/useResource.js';
import { CellStatus, asField, canProve, evidenceDepth } from '../evidence.jsx';
import { ErrorNote, Empty, Segmented, Skeleton } from '../ui.jsx';
import { host, num, truncate } from '../../lib/format.js';

/**
 * The record ledger.
 *
 * This is the dataset's main instrument rather than a table at the bottom of a
 * page, so it does three things the old version did not.
 *
 * 1. It is reachable from the overview as well as from the Records section, so
 *    the composition of the canvas changes while the ledger itself stays one
 *    implementation with one set of semantics.
 *
 * 2. A row expands in place. The old table only ever opened the right-hand
 *    inspector, which meant the answer to "what is on this record" was never on
 *    the canvas — you had to look away from the rows to see the fields of the
 *    row you just clicked. Expanding inline keeps the row and its evidence in
 *    the same eye movement.
 *
 * 3. Clicking a row still opens the inspector as well. The inline panel is a
 *    summary a reader can scan; the inspector is where the full evidence chain
 *    lives. Removing the inspector hop would have broken the one interaction
 *    the product is actually built around.
 *
 * TWO TARGETS, TWO QUESTIONS
 *
 * A cell is a button and the row is a button, because they answer different
 * questions: "why is this value like that" and "what is on this record". The
 * cell wins the click when it is under the pointer, which is why it stops
 * propagation.
 *
 * A cell with nothing behind it still renders, but is not a button. Offering to
 * open an evidence drawer that will report no evidence is worse than not
 * offering, because it teaches the reader the affordance is unreliable.
 */

const PAGE = 50;

/** One cell in the ledger. See the note on the two targets above. */
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
        {body}
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
      {/* Faint until the row is under the pointer, so a dense table is not a
          field of arrows. */}
      <span
        aria-hidden="true"
        className="mt-px shrink-0 text-[9px] text-accent opacity-0 transition-opacity duration-150 group-hover/cell:opacity-100 group-focus-visible/cell:opacity-100"
      >
        ⇥
      </span>
    </button>
  );
}

/**
 * The expanded row — the record's own fields, laid out beside the table.
 *
 * What it shows is exactly what is stored. There is no summary sentence
 * claiming a value is "confirmed across sources" because a record's fields each
 * carry a single source; the honest statement is the one below, which counts
 * how much of this record can be re-checked against a stored page.
 */
function ExpandedRow({ record, columns, ordinal, onInspect, onCell }) {
  const cells = columns
    .map((c) => ({ name: c, pf: asField(record?.fields?.[c]) }))
    .filter((c) => c.pf.value !== null && c.pf.value !== undefined && c.pf.value !== '');

  const provable = cells.filter((c) => canProve(c.pf.source)).length;

  return (
    <tr className="bg-warm/25">
      <td colSpan={columns.length} className="px-3.5 py-3">
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="min-w-0 space-y-2.5">
            {cells.length ? (
              <dl className="grid gap-x-5 gap-y-1.5 sm:grid-cols-2">
                {cells.map(({ name, pf }) => {
                  const src = pf.source;
                  const text =
                    typeof pf.value === 'object'
                      ? Array.isArray(pf.value)
                        ? pf.value.join(', ')
                        : JSON.stringify(pf.value)
                      : String(pf.value);
                  return (
                    <div key={name} className="min-w-0">
                      <dt className="eyebrow flex items-center gap-1.5">
                        <CellStatus status={pf.status} size="sm" />
                        {name}
                      </dt>
                      <dd className="mt-0.5 break-words text-[12.5px] leading-snug text-ink">
                        {truncate(text, 300)}
                      </dd>
                      <dd className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-muted">
                        {src?.url ? (
                          <a
                            href={src.url}
                            target="_blank"
                            rel="noreferrer noopener"
                            onClick={(e) => e.stopPropagation()}
                            className="link font-mono"
                          >
                            {host(src.url)} ↗
                          </a>
                        ) : (
                          <span className="text-warn">no source recorded</span>
                        )}
                        {canProve(src) ? (
                          <span className="text-ok">re-locates in the stored page</span>
                        ) : null}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            ) : (
              <p className="text-[12px] text-muted">
                This record holds no values — extraction produced an empty payload for it.
              </p>
            )}

            {/* The one derived claim on this panel, and it is arithmetic. */}
            <p className="border-t border-rule pt-2 text-[10.5px] leading-snug text-muted">
              {provable} of {cells.length}{' '}
              {cells.length === 1 ? 'value' : 'values'} on this record can be re-checked against the
              stored page text at the cited offsets.
            </p>
          </div>

          <div className="flex shrink-0 items-start gap-1.5 lg:flex-col">
            <button
              type="button"
              className="btn-outline btn-xs whitespace-nowrap"
              onClick={(e) => {
                e.stopPropagation();
                onInspect();
              }}
            >
              Full evidence
            </button>
            <button
              type="button"
              className="btn-ghost btn-xs whitespace-nowrap"
              onClick={(e) => {
                e.stopPropagation();
                onCell(record, columns[0], ordinal - 1);
              }}
            >
              Inspect a field
            </button>
          </div>
        </div>
      </td>
    </tr>
  );
}

export default function RecordsTable({
  datasetId,
  schema,
  onPick,
  onCell,
  pageSize = PAGE,
  className = '',
}) {
  const [page, setPage] = useState(0);
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  // Which row is open inline, keyed by its index within the current page. `null`
  // means none, and it is deliberately not a set: opening a second row closes
  // the first, because two expanded rows in a 50-row ledger reads as a layout
  // bug rather than as an inspection.
  const [openRow, setOpenRow] = useState(null);

  useEffect(() => {
    const id = setTimeout(() => {
      setDebounced(q);
      setPage(0);
      setOpenRow(null);
    }, 300);
    return () => clearTimeout(id);
  }, [q]);

  const { data, status, error, refetch, isStale } = useResource(
    (o) =>
      api.records(datasetId, { limit: pageSize, offset: page * pageSize, q: debounced }, o),
    [datasetId, page, debounced, pageSize]
  );

  const rows = useMemo(() => data?.records || [], [data]);
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));

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
    if (statusFilter === 'proven') {
      return rows.filter((r) => {
        const fs = Object.values(r?.fields || {});
        return fs.length && fs.every((f) => f?.verification_status === 'verified');
      });
    }
    return rows.filter((r) =>
      Object.values(r?.fields || {}).some((f) => f?.verification_status === statusFilter)
    );
  }, [rows, statusFilter]);

  const ordinalOf = (r) => page * pageSize + rows.indexOf(r) + 1;

  return (
    <div className={`space-y-3 ${className}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented
          size="xs"
          label="Record status filter"
          value={statusFilter}
          onChange={(v) => {
            setStatusFilter(v);
            setOpenRow(null);
          }}
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
                  research table is ruled paper, not a set of stacked panels. */}
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
                  {shown.map((r, i) => {
                    const open = openRow === i;
                    return (
                      <RecordRowPair
                        key={`${page}-${i}-${String(r?.record_id ?? i)}`}
                        open={open}
                        record={r}
                        columns={columns}
                        index={i}
                        ordinal={ordinalOf(r)}
                        onPick={onPick}
                        onCell={onCell}
                        onToggle={() => setOpenRow(open ? null : i)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>

            <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-rule px-3.5 py-2">
              <span className="text-[11px] text-muted">
                {isStale ? 'refreshing… · ' : ''}
                showing {(page * pageSize + 1).toLocaleString()}–
                {(page * pageSize + rows.length).toLocaleString()} of{' '}
                <span className="font-mono tnum text-ink-2">{num(total)}</span>
                {statusFilter !== 'all' ? (
                  <span className="text-warn"> · filtered to {shown.length} on this page</span>
                ) : null}
              </span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  disabled={page === 0}
                  onClick={() => {
                    setPage((p) => Math.max(0, p - 1));
                    setOpenRow(null);
                  }}
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
                  onClick={() => {
                    setPage((p) => p + 1);
                    setOpenRow(null);
                  }}
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

/**
 * A row and, when it is open, the row beneath it.
 *
 * A `<Fragment>` is used rather than rendering the expanded panel as a nested
 * block inside the `<td>`, because a table row that grows tall pushes every
 * row below it down the page and the reader loses their place in the ledger. A
 * second `<tr>` with a full-width cell keeps the grid readable and lets the
 * expanded content use its own two-column layout.
 */
function RecordRowPair({ open, record, columns, index, ordinal, onPick, onCell, onToggle }) {
  return (
    <>
      <tr
        onClick={() => onPick(record, ordinal - 1)}
        className="row cursor-pointer text-[12px] text-ink-2 hover:text-ink"
        style={{ animation: `rise-in .3s var(--e-swift) both`, animationDelay: `${Math.min(index, 14) * 14}ms` }}
      >
        {columns.map((c, ci) => (
          <td key={c} className="max-w-[280px] px-3.5 py-2.5 align-top leading-snug">
            <div className="flex items-start gap-1">
              {/*
                The disclosure sits in the first column only. Giving every row
                its own button in a 12-column table means 12 tab stops per row,
                which is the thing that makes a dense ledger unusable by
                keyboard; one stop per row is the accessible version.
              */}
              {ci === 0 ? (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggle();
                  }}
                  aria-expanded={open}
                  aria-label={open ? `Collapse record ${ordinal}` : `Expand record ${ordinal}`}
                  className="focusable -ml-1 mt-px w-3 shrink-0 text-[9px] text-accent transition-transform duration-200 ease-swift"
                  style={{ transform: open ? 'rotate(90deg)' : 'none' }}
                >
                  ▶
                </button>
              ) : null}
              <Cell
                name={c}
                prov={record?.fields?.[c]}
                onOpen={() => onCell(record, c, ordinal - 1)}
              />
            </div>
          </td>
        ))}
      </tr>
      {open ? (
        <ExpandedRow
          record={record}
          columns={columns}
          ordinal={ordinal}
          onInspect={() => onPick(record, ordinal - 1)}
          onCell={onCell}
        />
      ) : null}
    </>
  );
}