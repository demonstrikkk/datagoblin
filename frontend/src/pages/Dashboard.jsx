import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import {
  Empty,
  ErrorNote,
  Loading,
  Notice,
  Pill,
  StackedBar,
} from '../components/ui.jsx';
import { VoiceEmpty } from '../components/evidence.jsx';
import { num, toneVar, when } from '../lib/format.js';

/**
 * The provenance story.
 *
 * The previous version of this page opened with four stat cards: sources,
 * records, values, proven. Accurate, and unreadable — four numbers of the same
 * shape stacked in a row is an admin panel, and a percentage with nothing to
 * compare it against does not tell you whether to worry.
 *
 * What this page is actually for is one question: how much of what I am looking
 * at can be defended, and where did it come from. So it leads with a sentence,
 * then the one shape that carries the answer — the share of values by how far
 * each got — then which sites paid, then the datasets themselves.
 *
 * Every figure comes from the same server-side aggregate the API serves, so this
 * page and the dataset page cannot disagree.
 */
export default function Dashboard() {
  const { data, status, error, refetch, isStale } = useResource(
    (o) => api.dashboard({ limit: 25 }, o),
    []
  );
  const { inspect } = useInspector();
  const [sort, setSort] = useState('proven');

  const t = data?.totals || null;
  const datasets = useMemo(() => {
    const rows = Array.isArray(data?.datasets) ? [...data.datasets] : [];
    const by = {
      proven: (a, b) => (b?.coverage?.verified || 0) - (a?.coverage?.verified || 0),
      coverage: (a, b) => (b?.coverage_pct || 0) - (a?.coverage_pct || 0),
      records: (a, b) => (b?.records || 0) - (a?.records || 0),
      gaps: (a, b) => (b?.empty_fields || 0) - (a?.empty_fields || 0),
    };
    return rows.sort(by[sort] || by.proven);
  }, [data, sort]);

  if (status === 'loading' && !data) return <Loading label="Reading stored evidence" rows={4} />;
  if (error && !data) return <ErrorNote error={error} onRetry={refetch} />;
  if (!data) {
    return (
      <VoiceEmpty
        state="noDatasets"
        action={
          <Link to="/" className="btn-primary btn-xs">
            Start an investigation
          </Link>
        }
      />
    );
  }

  const cells = t?.cells || 0;
  const nDatasets = data.datasets_summarised || 0;

  const bands = [
    { key: 'verified', n: t?.verified || 0, tone: 'ok', label: 'proven', hint: 'the value sits inside its own quote, and that quote re-locates in the stored page' },
    { key: 'unverified', n: t?.unverified || 0, tone: 'info', label: 'unresolved', hint: 'a quote was captured, but no judge has ruled on it' },
    { key: 'conflicting', n: t?.conflicting || 0, tone: 'judge', label: 'disputed', hint: 'two sources disagree about this value' },
    { key: 'missing', n: t?.missing || 0, tone: 'muted', label: 'never filled', hint: 'a field the schema asked for that no record carries' },
  ].filter((b) => b.n > 0);

  /* The pipeline, left to right, each stage's width being what survived it.
     On an honest dataset the last stage is a small fraction of the one before
     it, and that ratio is the product's whole claim. */
  const stages = [
    { key: 'sources', label: 'Sources fetched', value: t?.sources || 0, sub: `${num(t?.source_ok || 0)} ok` },
    { key: 'records', label: 'Records', value: t?.records || 0, sub: 'survived dedupe' },
    { key: 'cells', label: 'Values', value: cells, sub: 'fields × records' },
    { key: 'proven', label: 'Proven', value: t?.verified || 0, sub: 'quote re-locates in the page' },
  ];

  return (
    <div className="space-y-7">
      {/* The headline. One statement about the whole workspace, then the
          numbers that make it checkable underneath. */}
      <header className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <p className="eyebrow">
              {nDatasets} dataset{nDatasets === 1 ? '' : 's'}
              {cells ? ` · ${num(cells)} values` : ''}
            </p>
            <h1 className="mt-1.5 h-display text-[30px] leading-[1.1] text-ink balance">
              {cells ? 'The web left a trail.' : 'Nothing carried back yet.'}
            </h1>
          </div>
          <button type="button" className="btn-outline btn-xs" onClick={refetch}>
            {isStale ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>

        {cells ? (
          <p className="max-w-2xl font-display text-[17px] leading-[1.45] text-ink-2">
            {num(t?.verified || 0)} of {num(cells)} values have a source behind them
            {t?.unverified ? `, and ${num(t.unverified)} still need a decision` : ''}
            {t?.conflicting ? `. ${num(t.conflicting)} disagree with another source` : ''}.
          </p>
        ) : null}
      </header>

      <ProvenanceRibbon stages={stages} />

      {cells ? (
        <section className="surface p-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="eyebrow">Provenance</h2>
            <span className="text-[11px] text-muted">
              {t?.proven_pct || 0}% of every value trace back to a stored page
            </span>
          </div>
          <StackedBar height={12} total={cells} rows={bands} />
          <dl className="mt-3 grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            {bands.map((b) => (
              <div key={b.key} className="flex items-baseline gap-2" title={b.hint}>
                <span
                  className="h-1.5 w-1.5 shrink-0 translate-y-px rounded-full"
                  style={{ background: toneVar(b.tone) }}
                  aria-hidden="true"
                />
                <dt className="text-[12px] text-ink-2">{b.label}</dt>
                <dd className="font-mono tnum text-[12px] text-ink">{num(b.n)}</dd>
              </div>
            ))}
          </dl>
        </section>
      ) : (
        <Notice tone="info">
          No values are stored yet, so there is nothing to summarise beyond the dataset list.
        </Notice>
      )}

      {data.unreadable_datasets > 0 ? (
        <Notice tone="warn">
          {data.unreadable_datasets} dataset
          {data.unreadable_datasets === 1 ? "'s records" : "s' records"} could not be read and{' '}
          {data.unreadable_datasets === 1 ? 'is' : 'are'} listed below without figures, rather
          than being dropped.
        </Notice>
      ) : null}

      {/*
        Source data is not repeated here.

        This page used to carry a "which sites paid" host ranking, and the
        dedicated Sources page carries the same ranking properly — filterable,
        with pages, text stored, runs touched and last used per host. Having it
        in both places meant two lists of the same hosts measured two different
        ways (proven values here, stored characters there), which is worse than
        either one: two numbers for the same thing invites a reader to assume
        the smaller one is a defect.

        So the dashboard says where the evidence came from in one line and
        points at the page that can be interrogated. Its own subject stays
        single: how much of what is in front of me can be defended.
      */}
      {data.top_hosts?.length ? (
        <section className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1.5 border-t border-rule pt-4">
          <p className="text-[12.5px] text-ink-2">
            <span className="text-ink">{data.top_hosts.length}</span> sites have supplied proven
            values, led by{' '}
            <span className="font-mono text-ink">{data.top_hosts[0]?.host}</span> with{' '}
            <span className="font-mono text-ink">{num(data.top_hosts[0]?.verified || 0)}</span>.
          </p>
          <Link to="/sources" className="link text-[12px]">
            Browse every stored source
          </Link>
        </section>
      ) : null}

      <section>
        <header className="mb-2 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="eyebrow">Datasets</h2>
            <p className="mt-0.5 text-[11.5px] text-muted">
              {data.truncated
                ? `First ${data.datasets_summarised} of ${data.datasets_available}`
                : 'Sorted by what you chose'}
            </p>
          </div>
          <div className="flex flex-wrap gap-1">
            {[
              { k: 'proven', l: 'Proven values' },
              { k: 'coverage', l: 'Most filled' },
              { k: 'records', l: 'Most records' },
              { k: 'gaps', l: 'Most never filled' },
            ].map((o) => (
              <button
                key={o.k}
                type="button"
                onClick={() => setSort(o.k)}
                aria-pressed={sort === o.k}
                className={`focusable rounded-xs border px-2 py-0.5 text-[11px] transition-colors duration-150 ease-swift ${
                  sort === o.k
                    ? 'border-ink bg-ink text-paper-2'
                    : 'border-rule-2 text-muted hover:border-ink hover:text-ink'
                }`}
              >
                {o.l}
              </button>
            ))}
          </div>
        </header>

        {datasets.length ? (
          <div className="surface overflow-x-auto">
            <table className="w-full min-w-max border-collapse text-left">
              <thead>
                <tr className="border-b border-rule-2">
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Dataset</th>
                  <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Records</th>
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Provenance</th>
                  <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Proven</th>
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Never filled</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {datasets.map((d) => (
                  <tr
                    key={d.dataset_id}
                    onClick={() =>
                      inspect({ kind: 'event', event: d, title: d.name || d.dataset_id })
                    }
                    className="row cursor-pointer text-[12px] text-ink-2 hover:text-ink"
                  >
                    <td className="px-3.5 py-2.5">
                      <span className="flex items-center gap-1.5">
                        <Link
                          to={`/library/${d.dataset_id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="link"
                        >
                          {d.name || d.dataset_id.slice(0, 8)}
                        </Link>
                        {d.sampled ? (
                          <Pill tone="info" title="Figures are computed from the first 2000 records.">
                            sampled
                          </Pill>
                        ) : null}
                        {d.partial ? (
                          <Pill tone="warn" title="The run stopped before it finished.">
                            partial catch
                          </Pill>
                        ) : null}
                      </span>
                      {d.created_at ? (
                        <span className="mt-0.5 block text-[10.5px] text-muted">
                          {when(d.created_at)}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-3.5 py-2.5 text-right font-mono tnum">
                      {num(d.records || 0)}
                    </td>
                    {d.coverage == null ? (
                      <td colSpan={3} className="px-3.5 py-2.5">
                        <Pill tone="warn" title={d.reason || ''}>
                          unreadable
                        </Pill>
                      </td>
                    ) : (
                      <>
                        <td className="px-3.5 py-2.5" style={{ minWidth: 140 }}>
                          <StackedBar
                            height={6}
                            total={d.records || 1}
                            rows={[
                              { key: 'verified', n: d.coverage.verified, tone: 'ok', label: 'Proven' },
                              { key: 'unverified', n: d.coverage.unverified, tone: 'info', label: 'Unresolved' },
                              { key: 'conflicting', n: d.coverage.conflicting, tone: 'judge', label: 'Disputed' },
                              { key: 'missing', n: d.coverage.missing, tone: 'muted', label: 'Empty' },
                            ].filter((r) => r.n > 0)}
                          />
                        </td>
                        <td className="px-3.5 py-2.5 text-right font-mono tnum">
                          {d.proven_pct}%
                        </td>
                        <td className="px-3.5 py-2.5">
                          {d.empty_fields > 0 ? (
                            <Pill tone="warn" title="Declared by the schema but filled on no record.">
                              {num(d.empty_fields)}
                            </Pill>
                          ) : d.partial_fields > 0 ? (
                            <Pill
                              tone="info"
                              title="Filled on some records and empty on others — a top-up closes these."
                            >
                              {num(d.partial_fields)} partial
                            </Pill>
                          ) : (
                            <span className="text-muted">none</span>
                          )}
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <VoiceEmpty
            state="noDatasets"
            action={
              <Link to="/" className="btn-primary btn-xs">
                Start an investigation
              </Link>
            }
          />
        )}
      </section>
    </div>
  );
}

/**
 * The pipeline as one shape: each stage's width is what survived it.
 *
 * The gap between "values" and "proven" is the honest answer to "is this
 * trustworthy", and on a real dataset it is large. Drawing the stages
 * proportionally makes that visible before a single number is read, which is why
 * this is a ribbon and not four stat cards.
 */
function ProvenanceRibbon({ stages }) {
  const max = Math.max(...stages.map((s) => s.value), 1);
  return (
    <ol className="flex items-stretch gap-2" aria-label="What survived each stage">
      {stages.map((s, i) => {
        const w = Math.max(0.1, s.value / max);
        return (
          <li key={s.key} className="min-w-0 flex-1">
            <div
              className="relative h-10 overflow-hidden rounded-sm bg-warm/60"
              title={`${s.label}: ${num(s.value)}`}
            >
              <div
                className="absolute inset-y-0 left-0 rounded-l-sm transition-all duration-500 ease-swift"
                style={{
                  width: `${w * 100}%`,
                  background: 'rgb(var(--accent))',
                  opacity: 0.92 - i * 0.15,
                }}
              />
              {/* The figure sits at the end of its own bar, so the number and
                  the length it describes read as one object. When the bar
                  fills the cell there is no room outside it, so the label goes
                  inside against the copper — and flips to paper fill, because
                  copper-on-copper at this size is unreadable. Clamping the
                  offset is what stops `8,318` being sliced in half by the edge
                  of a nearly-full cell. */}
              <span
                className="absolute top-1/2 -translate-y-1/2 whitespace-nowrap text-[11.5px] font-medium tabular-nums"
                style={
                  w > 0.8
                    ? { right: 8, color: 'rgb(var(--paper))' }
                    : { left: `calc(${w * 100}% + 8px)`, color: 'rgb(var(--ink-2))' }
                }
              >
                {num(s.value)}
              </span>
            </div>
            <p className="mt-1 truncate text-[11px] font-medium text-ink" title={s.label}>
              {s.label}
            </p>
            <p className="truncate text-[10px] text-muted" title={s.sub}>
              {s.sub}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
