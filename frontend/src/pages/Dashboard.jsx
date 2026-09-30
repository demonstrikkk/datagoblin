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
  Section,
  StackedBar,
  Stat,
  Treemap,
} from '../components/ui.jsx';
import { num } from '../lib/format.js';

/**
 * Is any of this working?
 *
 * The library answers "what datasets do I have" and the run list answers "what
 * ran". Neither answers the question someone actually opens the app to ask, and
 * the previous version of this page answered it with a row of numbers and two
 * tables — accurate, and unreadable, because a percentage with nothing to
 * compare it against does not tell you whether to worry.
 *
 * So it is built as the pipeline, left to right, and the width of each stage is
 * the number of things that survived it. Sources become pages become records
 * become cells become *proven* cells, and the last stage is a small fraction of
 * the one before it on any honest dataset — that ratio is the product's whole
 * claim, and it should be the first thing on the page rather than buried in a
 * tooltip. Every figure comes from the same server-side aggregate the API
 * serves, so this page and the dataset page cannot disagree.
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
    return <Empty title="Nothing to summarise yet" hint="Run a plan to build a dataset." />;
  }

  const cells = t?.cells || 0;
  const stages = [
    { key: 'sources', label: 'Sources fetched', value: t?.sources || 0, sub: `${t?.source_ok || 0} ok` },
    { key: 'records', label: 'Records', value: t?.records || 0, sub: 'survived dedupe' },
    { key: 'cells', label: 'Values', value: cells, sub: 'fields × records' },
    { key: 'proven', label: 'Proven', value: t?.verified || 0, sub: 'quote re-locates in the page' },
  ];

  return (
    <div className="page space-y-6">
      <Section
        title="Dashboard"
        sub={
          data.truncated
            ? `First ${data.datasets_summarised} of ${data.datasets_available} datasets`
            : `${data.datasets_summarised} dataset${data.datasets_summarised === 1 ? '' : 's'}`
        }
        right={
          <button type="button" className="btn btn-quiet" onClick={refetch}>
            {isStale ? 'Refreshing…' : 'Refresh'}
          </button>
        }
      >
        <ProvenanceRibbon stages={stages} />

        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="Proven of all values"
            value={`${t?.proven_pct || 0}%`}
            sub={`${num(t?.verified || 0)} of ${num(cells)}`}
            tone={t?.proven_pct >= 50 ? 'ok' : 'ink'}
          />
          <Stat
            label="Unproven"
            value={num(t?.unverified || 0)}
            sub="a quote exists, nothing judged it"
            tone="info"
          />
          <Stat
            label="Disputed"
            value={num(t?.conflicting || 0)}
            sub="sources disagree"
            tone={t?.conflicting ? 'judge' : 'ink'}
          />
          <Stat
            label="Never filled"
            value={num(t?.empty_fields || 0)}
            sub="fields on no record at all"
            tone={t?.empty_fields ? 'warn' : 'ink'}
          />
        </div>

        {cells ? (
          <div className="mt-4">
            <div className="mb-1 flex items-baseline justify-between text-[11px] text-muted">
              <span>Where the values are</span>
              <span className="tnum">{num(cells)} total</span>
            </div>
            <StackedBar
              height={10}
              total={cells}
              rows={[
                { key: 'verified', n: t.verified, tone: 'ok', label: 'Proven' },
                { key: 'unverified', n: t.unverified, tone: 'info', label: 'Unproven' },
                { key: 'conflicting', n: t.conflicting, tone: 'judge', label: 'Disputed' },
                { key: 'missing', n: t.missing, tone: 'muted', label: 'Empty' },
              ].filter((r) => r.n > 0)}
            />
          </div>
        ) : (
          <Notice tone="info">
            No values are stored yet, so there is nothing to summarise beyond the
            dataset list.
          </Notice>
        )}

        {data.unreadable_datasets > 0 && (
          <Notice tone="warn">
            {data.unreadable_datasets} dataset
            {data.unreadable_datasets === 1 ? "'s records" : "s' records"} could not
            be read and {data.unreadable_datasets === 1 ? 'is' : 'are'} listed below
            without figures, rather than being dropped.
          </Notice>
        )}
      </Section>

      {data.top_hosts?.length ? (
        <Section
          title="Which sites paid"
          sub="Ranked by proven values, read from the quotes your cells already carry"
        >
          <HostYield hosts={data.top_hosts} />
        </Section>
      ) : null}

      <Section title="Datasets" sub="Sorted by what you chose">
        <div className="mb-2 flex flex-wrap gap-1">
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
              className={`rounded-full border px-2.5 py-0.5 text-[11px] transition-colors duration-200 ease-swift focusable ${
                sort === o.k
                  ? 'border-ink bg-ink text-paper'
                  : 'border-rule-2 text-muted hover:border-ink hover:text-ink'
              }`}
            >
              {o.l}
            </button>
          ))}
        </div>
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Dataset</th>
                <th className="num">Records</th>
                <th>Values</th>
                <th className="num">Proven</th>
                <th>Never filled</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr
                  key={d.dataset_id}
                  onClick={() =>
                    inspect({ kind: 'dataset', id: d.dataset_id, label: d.name || d.dataset_id })
                  }
                  style={{ cursor: 'pointer' }}
                >
                  <td>
                    <Link
                      to={`/library/${d.dataset_id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="link"
                    >
                      {d.name || d.dataset_id.slice(0, 8)}
                    </Link>
                    {d.sampled && (
                      <Pill tone="info" title="Figures are computed from the first 2000 records.">
                        sampled
                      </Pill>
                    )}
                  </td>
                  <td className="num mono">{num(d.records || 0)}</td>
                  {d.coverage == null ? (
                    <td colSpan={3}>
                      <Pill tone="warn" title={d.reason || ''}>
                        unreadable
                      </Pill>
                    </td>
                  ) : (
                    <>
                      <td style={{ minWidth: 130 }}>
                        <StackedBar
                          height={6}
                          total={d.records || 1}
                          rows={[
                            { key: 'verified', n: d.coverage.verified, tone: 'ok', label: 'Proven' },
                            { key: 'unverified', n: d.coverage.unverified, tone: 'info', label: 'Unproven' },
                            { key: 'conflicting', n: d.coverage.conflicting, tone: 'judge', label: 'Disputed' },
                            { key: 'missing', n: d.coverage.missing, tone: 'muted', label: 'Empty' },
                          ].filter((r) => r.n > 0)}
                        />
                      </td>
                      <td className="num mono">
                        {d.proven_pct}%
                        {d.conflicting > 0 && (
                          <Pill tone="judge" title={`${d.conflicting} value(s) have sources that disagree`}>
                            {d.conflicting}
                          </Pill>
                        )}
                      </td>
                      <td>
                        {d.empty_fields > 0 ? (
                          <Pill tone="warn" title="Declared by the schema but filled on no record.">
                            {d.empty_fields}
                          </Pill>
                        ) : d.partial_fields > 0 ? (
                          <Pill tone="info" title="Filled on some records and empty on others — a top-up closes these.">
                            {d.partial_fields} partial
                          </Pill>
                        ) : (
                          <span className="muted">none</span>
                        )}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!datasets.length && (
          <Empty title="No datasets yet" hint="Compile a plan and run it; this page fills in from what gets stored." />
        )}
      </Section>
    </div>
  );
}

/**
 * The pipeline as one shape: each stage's width is what survived it.
 *
 * The gap between "values" and "proven" is the honest answer to "is this
 * trustworthy", and on a real dataset it is large — most values carry a quote
 * that nothing has judged. Drawing the stages proportionally makes that visible
 * before a single number is read, which is the whole reason this is a ribbon
 * rather than four stat cards.
 */
function ProvenanceRibbon({ stages }) {
  const max = Math.max(...stages.map((s) => s.value), 1);
  return (
    <ol className="flex items-stretch gap-1.5">
      {stages.map((s, i) => {
        const w = Math.max(0.12, s.value / max);
        return (
          <li key={s.key} className="min-w-0 flex-1">
            <div
              className="relative h-11 overflow-hidden rounded-sm bg-warm"
              title={`${s.label}: ${num(s.value)}`}
            >
              <div
                className="absolute inset-y-0 left-0 transition-all duration-500 ease-swift"
                style={{
                  width: `${w * 100}%`,
                  background: 'var(--accent)',
                  opacity: 0.9 - i * 0.14,
                }}
              />
              <span className="absolute inset-y-0 right-2 flex items-center text-[11px] font-medium text-ink-2">
                {num(s.value)}
              </span>
            </div>
            <p className="mt-1 truncate text-[11px] text-ink" title={s.label}>
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

/**
 * Host yield as a treemap rather than a table.
 *
 * The interesting fact about a crawl is not which sites were visited, it is how
 * unevenly they paid — one site produced 226 proven values from two pages while
 * every other host produced none. A ranked list makes that a number to compare;
 * tiles sized by share make it the shape of the whole thing at a glance.
 */
function HostYield({ hosts }) {
  const top = hosts.slice(0, 10);
  const rest = hosts.slice(10);
  const items = top.map((h, i) => ({
    key: h.host,
    label: h.host,
    value: Math.max(1, h.verified || 0),
    n: h.verified || 0,
    // Ranked by area, but shaded by whether anything was proven at all, so a
    // site that was fetched and gave nothing is visibly a failure rather than
    // just a small tile.
    tone: h.verified ? (i < 3 ? undefined : 'info') : 'muted',
  }));
  if (rest.length) {
    const restVerified = rest.reduce((a, h) => a + (h.verified || 0), 0);
    if (restVerified > 0) {
      items.push({
        key: '__rest',
        label: `${rest.length} more`,
        value: restVerified,
        n: restVerified,
        tone: 'info',
      });
    }
  }
  return (
    <div className="grid gap-4 sm:grid-cols-[1.5fr_1fr]">
      <Treemap items={items} height={168} />
      <ul className="space-y-1 self-center">
        {hosts.slice(0, 8).map((h) => (
          <li
            key={h.host}
            className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-baseline gap-2 text-[11.5px]"
          >
            <span className="truncate font-mono text-ink-2" title={h.host}>
              {h.host}
            </span>
            <span className="tnum text-muted">{num(h.verified)}</span>
            <span className="tnum w-16 text-right text-muted" title="Proven values this host supplied">
              {num(h.verified)} proven
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
