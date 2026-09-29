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
} from '../components/ui.jsx';
import { num } from '../lib/format.js';

/**
 * One page that says whether any of this is working.
 *
 * The library answers "what datasets do I have" and the run list answers "what
 * ran". Neither answers the question someone actually opens the app to ask, and
 * answering it by fetching four pages into the browser was rejected: a summary
 * recomputed client-side shows different numbers than the API and shows a
 * loading state where the answer is one request away. So this reads one
 * aggregate and renders it.
 *
 * The headline number is proven cells over all cells, with unverified reported
 * beside it rather than folded in. A dataset where every value has a quote and
 * no verdict looks complete and is not, and a dashboard that reported it as
 * complete would be the least honest number on the page.
 */
export default function Dashboard() {
  const { data, status, error, refetch, isStale } = useResource(
    (o) => api.dashboard({ limit: 25 }, o),
    []
  );
  const { inspect } = useInspector();
  const [sort, setSort] = useState('proven');

  const t = data?.totals || null;

  // Sorting is by a real column of a real number, and the two orderings a user
  // might want disagree for a reason worth being explicit about: by proven
  // cells first puts the dataset that has actually been verified on top, while
  // by coverage first puts the one with the fewest holes on top even if very
  // little of it is proven.
  const datasets = useMemo(() => {
    const rows = Array.isArray(data?.datasets) ? [...data.datasets] : [];
    if (sort === 'proven') {
      rows.sort((a, b) => (b?.coverage?.verified || 0) - (a?.coverage?.verified || 0));
    } else if (sort === 'coverage') {
      rows.sort((a, b) => (b?.coverage_pct || 0) - (a?.coverage_pct || 0));
    } else if (sort === 'records') {
      rows.sort((a, b) => (b?.records || 0) - (a?.records || 0));
    } else if (sort === 'conflicts') {
      rows.sort((a, b) => (b?.conflicting || 0) - (a?.conflicting || 0));
    }
    return rows;
  }, [data, sort]);

  if (status === 'loading' && !data) return <Loading label="Reading stored evidence" rows={4} />;
  if (error && !data) {
    return <ErrorNote error={error} onRetry={refetch} />;
  }
  if (!data) return <Empty title="Nothing to summarise yet" hint="Run a plan to build a dataset." />;

  const rows = t?.cells
    ? [
        { key: 'verified', n: t.verified, tone: 'ok', label: 'Proven' },
        { key: 'unverified', n: t.unverified, tone: 'info', label: 'Unverified' },
        { key: 'conflicting', n: t.conflicting, tone: 'judge', label: 'Conflicting' },
        { key: 'missing', n: t.missing, tone: 'muted', label: 'Empty' },
      ].filter((r) => r.n > 0)
    : [];

  return (
    <div className="page">
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
        <div className="stat-row">
          <Stat label="Records" value={num(t?.records || 0)} sub={`${t?.datasets || 0} datasets`} />
          <Stat
            label="Proven cells"
            value={num(t?.verified || 0)}
            sub={`${t?.proven_pct || 0}% of all cells`}
            tone={t?.proven_pct >= 50 ? 'ok' : 'ink'}
          />
          <Stat label="Unverified" value={num(t?.unverified || 0)} sub="have a quote, no verdict" tone="info" />
          <Stat
            label="Conflicting"
            value={num(t?.conflicting || 0)}
            sub="sources disagree"
            tone={t?.conflicting ? 'judge' : 'ink'}
          />
          <Stat
            label="Empty fields"
            value={num(t?.empty_fields || 0)}
            sub="declared, never extracted"
            tone={t?.empty_fields ? 'warn' : 'ink'}
          />
          <Stat
            label="Sources"
            value={num(t?.sources || 0)}
            sub={`${num(t?.source_ok || 0)} ok or reused`}
          />
        </div>

        {rows.length ? (
          <div className="bar-block">
            <div className="bar-label">
              <span>Where the cells are</span>
              <span className="muted">
                {num(t?.cells || 0)} total
              </span>
            </div>
            <StackedBar rows={rows} total={t?.cells || 0} height={10} />
          </div>
        ) : (
          <Notice tone="info">
            No cells are stored yet, so there is nothing to summarise beyond the
            dataset list below.
          </Notice>
        )}

        {data.unreadable_datasets > 0 && (
          <Notice tone="warn">
            {data.unreadable_datasets} dataset{data.unreadable_datasets === 1 ? "'s records" : "s' records"}{' '}
            could not be read and {data.unreadable_datasets === 1 ? 'is' : 'are'} listed
            below without coverage figures, rather than being dropped.
          </Notice>
        )}
      </Section>

      <Section title="Datasets" sub="One row per dataset, read from its own stored records">
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th
                  className="sortable"
                  onClick={() => setSort('records')}
                  aria-sort={sort === 'records' ? 'descending' : 'none'}
                >
                  Dataset
                </th>
                <th
                  className="num sortable"
                  onClick={() => setSort('records')}
                  aria-sort={sort === 'records' ? 'descending' : 'none'}
                >
                  Records
                </th>
                <th
                  className="num sortable"
                  onClick={() => setSort('coverage')}
                  aria-sort={sort === 'coverage' ? 'descending' : 'none'}
                >
                  Filled
                </th>
                <th
                  className="num sortable"
                  onClick={() => setSort('proven')}
                  aria-sort={sort === 'proven' ? 'descending' : 'none'}
                >
                  Proven
                </th>
                <th
                  className="num sortable"
                  onClick={() => setSort('conflicts')}
                  aria-sort={sort === 'conflicts' ? 'descending' : 'none'}
                >
                  Conflicts
                </th>
                <th>Empty fields</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => (
                <tr
                  key={d.dataset_id}
                  onClick={() => inspect({ kind: 'dataset', id: d.dataset_id, label: d.name || d.dataset_id })}
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
                      <Pill tone="info" title="Coverage is computed from the first 2000 records, not every record.">
                        sampled
                      </Pill>
                    )}
                  </td>
                  <td className="num mono">{num(d.records || 0)}</td>
                  {d.coverage == null ? (
                    <td colSpan={4}>
                      <Pill tone="warn" title={d.reason || ''}>
                        unreadable
                      </Pill>
                    </td>
                  ) : (
                    <>
                      <td className="num mono">{d.coverage_pct}%</td>
                      <td className="num mono">
                        {d.proven_pct}%
                        {d.conflicting > 0 && (
                          <Pill tone="judge" title={`${d.conflicting} cell(s) have sources that disagree`}>
                            {d.conflicting}
                          </Pill>
                        )}
                      </td>
                      <td className="mono">
                        {d.empty_fields > 0 ? (
                          <Pill tone="warn" title="Declared by the schema but never extracted from any record.">
                            {d.empty_fields} never extracted
                          </Pill>
                        ) : d.partial_fields > 0 ? (
                          <Pill tone="info" title="Filled on some records and empty on others — a top-up can close these.">
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
          <Empty
            title="No datasets yet"
            hint="Compile a plan and run it; this page fills in from what gets stored."
          />
        )}
      </Section>

      <Section
        title="Which sites actually paid"
        sub="Verified cells per page spent, learned from the quotes your cells already carry"
      >
        {data.top_hosts?.length ? (
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Host</th>
                  <th className="num">Records</th>
                  <th className="num">Proven cells</th>
                  <th className="num">Pages spent</th>
                  <th className="num">Proven per page</th>
                </tr>
              </thead>
              <tbody>
                {data.top_hosts.map((h) => (
                  <tr key={h.host}>
                    <td className="mono">{h.host}</td>
                    <td className="num mono">{num(h.records || 0)}</td>
                    <td className="num mono">{num(h.verified || 0)}</td>
                    <td className="num mono">{num(h.pages || 0)}</td>
                    <td className="num mono">{h.yield ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="No host has produced a verified cell yet"
            hint="A site with no evidence behind it is reported as unknown, not as useless."
          />
        )}
      </Section>
    </div>
  );
}
