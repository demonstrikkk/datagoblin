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
import { num, toneVar, truncate, when } from '../lib/format.js';

/** Display names for the domains the classifier can return. */
const SECTOR_LABEL = {
  stocks: 'Markets',
  healthcare: 'Clinical',
  education: 'Education',
  realestate: 'Property',
  energy: 'Energy',
  hr: 'Workforce',
};

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

  /**
   * Group the workspace by detected domain.
   *
   * A dataset the backend declined to classify is counted in `unclassified` and
   * left out of the grouping, rather than being folded into "other" — a bucket
   * called "other" that is really "we did not know" is the same absence reported
   * as a category.
   */
  const { sectors, unclassified } = useMemo(() => {
    const groups = new Map();
    let unknown = 0;
    for (const d of data?.datasets || []) {
      const key = d?.sector?.sector;
      if (!key) {
        unknown += 1;
        continue;
      }
      const cov = d.coverage || {};
      const g = groups.get(key) || {
        key,
        label: SECTOR_LABEL[key] || key,
        n: 0,
        records: 0,
        cells: 0,
        verified: 0,
        datasets: [],
      };
      g.n += 1;
      g.records += Number(d.records) || 0;
      g.cells += Number(cov.cells) || 0;
      g.verified += Number(cov.verified) || 0;
      g.datasets.push(d);
      groups.set(key, g);
    }
    return {
      sectors: [...groups.values()].sort((a, b) => b.n - a.n || b.records - a.records),
      unclassified: unknown,
    };
  }, [data]);

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

      {/*
        Domains, when the workspace has enough datasets to have a shape.

        This is a roll-up of the per-dataset classifier, so it inherits its
        refusal: a dataset the backend declined to classify is not guessed at
        here either. It appears only when at least two datasets carry a reading —
        one dataset is a case, not a distribution, and a one-row "domain
        breakdown" says nothing.
      */}
      {sectors.length >= 2 ? (
        <section>
          <header className="mb-2">
            <h2 className="eyebrow">Domains</h2>
            <p className="mt-0.5 text-[11.5px] text-muted">
              {unclassified > 0
                ? `${num(sectors.reduce((a, s) => a + s.n, 0))} of ${num(datasets.length)} datasets were classified; ${num(unclassified)} were not, and are not guessed at here`
                : 'What these datasets are, read from their field schemas'}
            </p>
          </header>
          <div className="surface overflow-hidden">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-rule-2">
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Domain</th>
                  <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Datasets</th>
                  <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Records</th>
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Proven</th>
                  <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Datasets</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {sectors.map((s) => (
                  <tr key={s.key} className="text-[12px]">
                    <td className="px-3.5 py-2 font-medium text-ink">{s.label}</td>
                    <td className="px-3.5 py-2 text-right font-mono tnum text-ink-2">
                      {num(s.n)}
                    </td>
                    <td className="px-3.5 py-2 text-right font-mono tnum text-ink-2">
                      {num(s.records)}
                    </td>
                    <td className="px-3.5 py-2" style={{ minWidth: 140 }}>
                      <StackedBar
                        height={6}
                        total={Math.max(1, s.cells)}
                        rows={[
                          { key: 'verified', n: s.verified, tone: 'ok', label: 'Proven' },
                          { key: 'unverified', n: Math.max(0, s.cells - s.verified), tone: 'muted', label: 'Unproven' },
                        ].filter((r) => r.n > 0)}
                      />
                    </td>
                    <td className="max-w-[380px] px-3.5 py-2">
                      <span className="flex flex-wrap gap-1">
                        {s.datasets.slice(0, 3).map((d) => (
                          <Link
                            key={d.dataset_id}
                            to={`/library/${d.dataset_id}`}
                            className="max-w-[120px] truncate rounded-xs border border-rule bg-warm/60 px-1.5 py-0.5 text-[10.5px] text-ink-2 transition-colors hover:border-ink hover:text-ink"
                            title={d.name || d.dataset_id}
                          >
                            {truncate(d.name || d.dataset_id.slice(0, 8), 24)}
                          </Link>
                        ))}
                        {s.datasets.length > 3 ? (
                          <span className="px-1 py-0.5 text-[10.5px] text-muted">
                            +{num(s.datasets.length - 3)} more
                          </span>
                        ) : null}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
                {datasets.map((d) => {
                  /* Zero records is a state of its own, decided from the
                     declared count rather than the sampled one so a dataset
                     whose records were read and found empty is not mislabelled. */
                  const isEmpty = Number(d.declared_records ?? d.records ?? 0) === 0;
                  return (
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
                        {isEmpty ? (
                          <Pill
                            tone="muted"
                            title="The run stored pages but extracted no records. Re-reading the stored pages can still fill it."
                          >
                            no records
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
                    ) : isEmpty ? (
                      /* A dataset that collected nothing is a different fact from
                         * a dataset whose values failed to verify, and the row has
                         * to say which it is. Both used to render as `0.0%` beside
                         * an empty bar, so a run that fetched pages and extracted
                         * nothing was indistinguishable from a run whose
                         * verification failed — which is the worse of the two to
                         * get wrong, because one is recoverable and the other
                         * looks like a broken install.
                       *
                       * 45% of this workspace is in exactly that state, all of it
                       * from before the extraction window was fixed, so the
                       * honest reading is "nothing was extracted", not "nothing
                       * verified". */
                      <td colSpan={3} className="px-3.5 py-2.5">
                        <span
                          className="text-xs text-muted"
                          title="This run stored pages but extracted no records. Re-reading those pages can still fill it."
                        >
                          nothing extracted
                        </span>
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
                  );
                })}
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
