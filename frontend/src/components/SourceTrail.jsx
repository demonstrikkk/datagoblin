import { useMemo } from 'react';
import { num, truncate, when } from '../lib/format.js';

/**
 * What the run is actually doing, as it does it.
 *
 * Two lists and a count, all read from the event stream. There is no node
 * graph and no pipeline animation: the stages are already a horizontal rail
 * (`PipelineRail`), and a graph of Search → Fetch → LLM → Database would draw a
 * system this product does not have.
 *
 * Everything here comes from events the backend really emitted. A source with
 * no outcome is shown as having no outcome rather than quietly dropped, and a
 * `source.reused` line says reused rather than fetched, because reusing stored
 * evidence and downloading a page are different acts and the run view should
 * not blur them.
 */

const OUTCOME = {
  fetched: { label: 'fetched', tone: 'ok', glyph: '✓' },
  reused: { label: 'reused', tone: 'info', glyph: '≡' },
  skipped: { label: 'skipped', tone: 'muted', glyph: '–' },
  failed: { label: 'failed', tone: 'danger', glyph: '✕' },
  pending: { label: 'no outcome yet', tone: 'muted', glyph: '·' },
};

/**
 * Collapse the stream into one row per source.
 *
 * A URL is keyed on the normalised origin+path, and later events override
 * earlier ones for the same URL, so a source that was fetched and then
 * re-fetched on a retry collapses to its final outcome rather than appearing
 * twice and inflating the count.
 *
 * `source.fetched` and `source.reused` both carry `data.url`. Where an event
 * predates that field the url is recovered from the message, and such a row is
 * marked `inferred` so the trail can say which entries are parsed from prose.
 */
export function buildSourceTrail(events) {
  const byKey = new Map();

  const keyFor = (url) => {
    try {
      const u = new URL(url);
      return `${u.hostname}${u.pathname}`.replace(/\/$/, '');
    } catch {
      return String(url || '').slice(0, 120);
    }
  };

  for (const e of events || []) {
    const t = e?.type;
    if (t !== 'source.fetched' && t !== 'source.reused' && t !== 'source.discovered') continue;

    // `source.discovered` is a summary event — one per discovery round carrying
    // a count, not one per URL. It is counted, never listed, because inventing
    // a row per accepted source would be a list this data cannot support.
    if (t === 'source.discovered') continue;

    const data = e.data || {};
    let url = data.url || null;
    let inferred = false;
    if (!url) {
      const m = String(e.message || '').match(/https?:\/\/\S+/);
      if (m) {
        url = m[0];
        inferred = true;
      }
    }
    if (!url) continue;

    const key = keyFor(url);
    const prior = byKey.get(key) || { url, key, at: e.timestamp, inferred };
    byKey.set(key, {
      ...prior,
      url,
      // A later structured event replaces an inferred one.
      inferred: prior.inferred && inferred,
      outcome: t === 'source.reused' ? 'reused' : data.ok === false ? 'skipped' : 'fetched',
      pageId: data.page_id || prior.pageId || null,
      reusedFrom: data.reused_from_run_id || prior.reusedFrom || null,
      chars: data.char_count ?? prior.chars ?? null,
      at: e.timestamp || prior.at,
    });
  }

  return [...byKey.values()].sort((a, b) => String(a.at || '').localeCompare(String(b.at || '')));
}

/** Total sources discovery reported, across every round of the run. */
export function discoveredTotal(events) {
  let n = 0;
  for (const e of events || []) {
    if (e?.type === 'source.discovered') n += Number(e.data?.count) || 0;
  }
  return n;
}

export function SourceTrail({ events, onPick, height = 430 }) {
  const rows = useMemo(() => buildSourceTrail(events), [events]);
  const discovered = useMemo(() => discoveredTotal(events), [events]);

  if (!rows.length) {
    return (
      <div className="surface p-3.5">
        <p className="eyebrow mb-2">Source trail</p>
        <p className="text-[11.5px] leading-relaxed text-muted">
          {discovered
            ? `Discovery reported ${num(discovered)} sources, but this run has not reported an outcome for any of them yet.`
            : 'No source has reported an outcome yet. Outcomes appear as pages are fetched or reused from storage.'}
        </p>
      </div>
    );
  }

  return (
    <div className="surface flex min-h-0 flex-col p-3.5">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <p className="eyebrow">Source trail</p>
        <span className="text-[10.5px] text-muted">
          {num(rows.length)} with an outcome
          {discovered ? ` · ${num(discovered)} discovered` : ''}
        </span>
      </div>

      <ol className="scroll-y -mx-1 min-h-0 flex-1" style={{ maxHeight: height }}>
        {rows.map((r) => {
          const o = OUTCOME[r.outcome] || OUTCOME.pending;
          return (
            <li key={r.key}>
              <button
                type="button"
                onClick={onPick ? () => onPick(r) : undefined}
                className={`row flex w-full items-baseline gap-2 rounded-xs px-1 py-1 text-left ${
                  onPick ? 'focusable cursor-pointer' : ''
                }`}
              >
                <span className={`shrink-0 text-[10px] ${o.tone === 'muted' ? 'text-muted' : ''}`}
                      style={o.tone === 'muted' ? undefined : { color: `rgb(var(--${o.tone}))` }}
                      title={o.label}
                      aria-hidden="true"
                >
                  {o.glyph}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-[11px] text-ink-2" title={r.url}>
                    {truncate(r.url, 68)}
                  </span>
                  <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-muted">
                    <span>{o.label}</span>
                    {r.chars ? <span>{num(r.chars)} chars</span> : null}
                    {r.reusedFrom ? (
                      <span title="The earlier run whose stored page was reused">
                        from run {String(r.reusedFrom).slice(0, 8)}
                      </span>
                    ) : null}
                    {r.pageId ? (
                      <span className="font-mono" title="Stored page id">
                        page {String(r.pageId).slice(0, 8)}
                      </span>
                    ) : null}
                    {r.inferred ? (
                      <span
                        className="text-warn"
                        title="This event carried no structured url, so the address was read out of its message text"
                      >
                        url from message
                      </span>
                    ) : null}
                    {r.at ? <span>{when(r.at)}</span> : null}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * Records as they arrive.
 *
 * `record.extracted` reports a count per page, not the records themselves, so
 * this is a per-page ledger of what came out of where — which is exactly the
 * claim the event supports. It says "12 records from moneycontrol.com", not
 * "Hindustan Unilever / HUL", because the payload does not carry the values.
 */
export function FindingList({ events, height = 430 }) {
  const rows = useMemo(() => {
    const out = [];
    (events || []).forEach((e, i) => {
      if (e?.type !== 'record.extracted') return;
      const d = e.data || {};
      out.push({
        key: `${i}-${e.timestamp}`,
        url: d.url || null,
        count: Number(d.count) || 0,
        provider: d.provider || null,
        at: e.timestamp,
      });
    });
    return out;
  }, [events]);

  const total = rows.reduce((a, r) => a + r.count, 0);

  if (!rows.length) {
    return (
      <div className="surface p-3.5">
        <p className="eyebrow mb-2">Findings</p>
        <p className="text-[11.5px] leading-relaxed text-muted">
          Nothing extracted yet. Records appear here as each page is read.
        </p>
      </div>
    );
  }

  return (
    <div className="surface flex min-h-0 flex-col p-3.5">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <p className="eyebrow">Findings</p>
        <span className="text-[10.5px] text-muted">
          {num(total)} from {num(rows.length)} {rows.length === 1 ? 'page' : 'pages'}
        </span>
      </div>
      <ol className="scroll-y -mx-1 min-h-0 flex-1 space-y-0.5" style={{ maxHeight: height }}>
        {rows.map((r) => (
          <li
            key={r.key}
            className="row flex items-baseline gap-2 rounded-xs px-1 py-1"
            style={{ animation: 'rise-in .3s var(--e-swift) both' }}
          >
            <span className="shrink-0 font-mono text-[12px] text-ink">{num(r.count)}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate font-mono text-[10.5px] text-ink-2" title={r.url || ''}>
                {r.url ? truncate(r.url, 52) : 'page'}
              </span>
              <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-muted">
                {r.provider ? <span>{r.provider}</span> : null}
                {r.at ? <span>{when(r.at)}</span> : null}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
