import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { ErrorNote, Loading, Notice, RankBars } from '../components/ui.jsx';
import { VoiceEmpty } from '../components/evidence.jsx';
import { bytes, num, when } from '../lib/format.js';

/**
 * The source memory.
 *
 * Every host this workspace has ever stored evidence from, and what each one
 * actually paid. This is the other half of the evidence rail: the rail proves a
 * single value, this shows where the workspace's evidence comes from overall,
 * and which sites turned out to be worth going back to.
 *
 * Everything is aggregate. A page is listed with its host, how many pages that
 * host supplied, how much text was stored from it, how many runs touched it and
 * when it was last used. The honest measure of a host is the *characters* it
 * contributed, not the number of URLs visited — a crawl that fetches forty
 * pages and yields four thousand characters has been told something the counts
 * alone would hide.
 */
export default function Sources() {
  const [q, setQ] = useState('');
  const { data, status, error, refetch } = useResource(
    (o) => api.sourceHosts({ limit: 500 }, o),
    []
  );

  const hosts = data?.hosts || [];
  const unsupported = data?.unsupported === true;

  /**
   * Filtered *and* ordered by the measure actually plotted.
   *
   * The API returns rows ordered by page count, but the bars are drawn by
   * characters — so an earlier version listed ngodarpan.gov.in (394 KB) above
   * economictimes.indiatimes.com (3.4 MB) and the bars grew downwards. A ranked
   * chart whose bars are not in ranked order reads as broken regardless of how
   * correct each bar is. One sort, applied once, for both the bars and the
   * table, so the two agree.
   */
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = needle
      ? hosts.filter((h) => String(h.host || '').toLowerCase().includes(needle))
      : hosts;
    return [...rows].sort(
      (a, b) => (Number(b.chars) || 0) - (Number(a.chars) || 0) || (Number(b.pages) || 0) - (Number(a.pages) || 0)
    );
  }, [hosts, q]);

  const totals = useMemo(
    () => ({
      pages: hosts.reduce((a, h) => a + (Number(h.pages) || 0), 0),
      chars: hosts.reduce((a, h) => a + (Number(h.chars) || 0), 0),
      runs: hosts.reduce((a, h) => a + (Number(h.runs) || 0), 0),
    }),
    [hosts]
  );

  /**
   * How concentrated the stored evidence is.
   *
   * This is the fact the page exists to answer. A host list of 72 rows tells
   * you what is there; "five of them hold 62% of everything you have stored"
   * tells you where to go next and how much the rest are worth. It is computed
   * from the rows, never asserted.
   */
  const concentration = useMemo(() => {
    const sorted = [...hosts].sort((a, b) => (Number(b.chars) || 0) - (Number(a.chars) || 0));
    const sum = sorted.reduce((a, h) => a + (Number(h.chars) || 0), 0);
    if (!sorted.length || !sum) return null;
    const top = sorted.slice(0, 5);
    const topChars = top.reduce((a, h) => a + (Number(h.chars) || 0), 0);
    return { sum, top, topShare: (topChars / sum) * 100 };
  }, [hosts]);

  const bars = useMemo(
    () =>
      filtered.map((h) => ({
        key: h.host || 'unknown',
        label: h.host || '—',
        value: Number(h.chars) || 0,
        extra: `${num(h.pages)} ${h.pages === 1 ? 'page' : 'pages'}`,
      })),
    [filtered]
  );

  if (status === 'loading' && !data) return <Loading label="Reading the source memory" rows={5} />;
  if (error && !data) return <ErrorNote error={error} onRetry={refetch} />;

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="eyebrow">
          {num(hosts.length)} host{hosts.length === 1 ? '' : 's'} · {num(totals.pages)} stored
          page{totals.pages === 1 ? '' : 's'} · {bytes(totals.chars)} of text · touched by{' '}
          {num(totals.runs)} run{totals.runs === 1 ? '' : 's'}
        </p>
        <h1 className="h-display text-[28px] leading-tight text-ink balance">
          {hosts.length ? 'Where the evidence came from' : 'No trail left behind.'}
        </h1>
        {hosts.length ? (
          <p className="max-w-2xl font-display text-[17px] leading-[1.45] text-ink-2">
            {concentration ? (
              <>
                The {concentration.top.length} busiest of {num(hosts.length)} hosts hold{' '}
                <span className="text-ink">{concentration.topShare.toFixed(0)}%</span> of the{' '}
                {bytes(totals.chars)} of text in storage, and the remaining{' '}
                {num(hosts.length - concentration.top.length)} together hold the other{' '}
                {(100 - concentration.topShare).toFixed(0)}%.
              </>
            ) : (
              <>
                {num(hosts.length)} host{hosts.length === 1 ? '' : 's'} have contributed stored
                evidence to this workspace.
              </>
            )}
          </p>
        ) : null}
      </header>

      {unsupported ? (
        <Notice tone="warn">
          This backend&rsquo;s repository has no cross-run source aggregate, so the browser has
          nothing to show. Per-run stored pages are still available from any run&rsquo;s page list.
        </Notice>
      ) : null}

      {!unsupported && !hosts.length ? (
        <VoiceEmpty
          state="noSources"
          action={
            <Link to="/" className="btn-primary btn-xs">
              Start an investigation
            </Link>
          }
        />
      ) : null}

      {hosts.length ? (
        <>
          <div className="flex items-center gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Filter hosts…"
              aria-label="Filter sources by host"
              className="input-sm input w-56"
            />
            <span className="text-[11px] text-muted">
              {q ? `${num(filtered.length)} of ${num(hosts.length)}` : null}
            </span>
          </div>

          {/*
            Ranked bars, not a treemap. Stored-text totals here span 2.0 MB down
            to 70 KB across 72 rows, and squarifying that produces a stack of
            hairlines — a barcode that answers nothing. The taper of a bar chart
            is the actual finding: a few hosts carry the evidence and the rest
            are a long thin tail.
          */}
          {bars.length ? (
            <section>
              <header className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h2 className="eyebrow">Who paid</h2>
                  <p className="mt-0.5 text-[11.5px] text-muted">
                    Share of the text each host contributed to storage
                  </p>
                </div>
              </header>
              <RankBars items={bars} total={concentration?.sum} limit={14} format={bytes} tailFormat={bytes} />
            </section>
          ) : null}

          <section>
            <header className="mb-2">
              <h2 className="eyebrow">Hosts</h2>
            </header>
            <div className="surface overflow-x-auto">
              <table className="w-full min-w-max border-collapse text-left">
                <thead>
                  <tr className="border-b border-rule-2">
                    <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Host</th>
                    <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Pages</th>
                    <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Text stored</th>
                    <th className="eyebrow px-3.5 py-2.5 text-right font-semibold" scope="col">Runs</th>
                    <th className="eyebrow px-3.5 py-2.5 font-semibold" scope="col">Last used</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-rule">
                  {filtered.map((h) => (
                    <tr key={h.host || 'unknown'} className="row text-[12px] text-ink-2">
                      <td className="px-3.5 py-2 font-mono text-ink">{h.host || '—'}</td>
                      <td className="px-3.5 py-2 text-right font-mono tnum">{num(h.pages)}</td>
                      <td className="px-3.5 py-2 text-right font-mono tnum">{bytes(h.chars)}</td>
                      <td className="px-3.5 py-2 text-right font-mono tnum">{num(h.runs)}</td>
                      <td className="px-3.5 py-2 text-[11px] text-muted">{when(h.last_used)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!filtered.length ? (
              <p className="mt-2 text-[11.5px] text-muted">
                No host matches &ldquo;{q}&rdquo;. The stored evidence is unchanged; only this
                filter is narrower.
              </p>
            ) : null}
          </section>
        </>
      ) : null}
    </div>
  );
}
