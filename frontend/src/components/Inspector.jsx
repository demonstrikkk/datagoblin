import { useEffect, useMemo, useRef, useState } from 'react';
import { useInspector } from '../lib/inspector.jsx';
import { Empty, ErrorNote, KeyVal, Pill, SkeletonLines, CopyButton } from './ui.jsx';
import { REC_VERIFY, RUN_STATUS, host, num, toneClass, truncate, when } from '../lib/format.js';

/* ------------------------------------------------------------- primitives */

function Head({ eyebrow, title, sub, onClose, actions }) {
  return (
    <header className="shrink-0 border-b border-rule px-4 pb-3 pt-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">{eyebrow}</p>
          <h2 className="mt-1 truncate text-[15px] font-medium leading-tight text-ink" title={title}>
            {title}
          </h2>
          {sub ? <p className="mt-1 text-[11.5px] text-muted">{sub}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close inspector"
            className="focusable grid h-7 w-7 place-items-center rounded-sm text-muted transition-all duration-150 hover:bg-warm hover:text-ink active:scale-90"
          >
            ✕
          </button>
        </div>
      </div>
    </header>
  );
}

/* ------------------------------------------------------------------ proof */

/**
 * Render a stored field value readably.
 *
 * Raw extraction output is often an unformatted number — `17000000000` for a
 * funding amount. Printing that verbatim is unreadable, so large integers get
 * thousands separators plus a compact gloss. The exact value stays in the
 * title attribute, because rounding a figure someone may rely on would be its
 * own kind of lie.
 */
function FieldValue({ raw }) {
  if (raw === null || raw === undefined || raw === '') {
    return <span className="text-rule-2">—</span>;
  }
  if (typeof raw === 'boolean') {
    return <span>{raw ? 'yes' : 'no'}</span>;
  }
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    const grouped = raw.toLocaleString('en-US');
    if (Math.abs(raw) >= 1e6) {
      return (
        <span title={grouped}>
          {grouped}
          <span className="ml-1.5 text-[11px] text-muted">
            ({(raw / 1e6).toLocaleString('en-US', { maximumFractionDigits: 1 })}M)
          </span>
        </span>
      );
    }
    return <span className="font-mono tnum">{grouped}</span>;
  }
  if (typeof raw === 'object') {
    return <span className="break-words">{Array.isArray(raw) ? raw.join(', ') : JSON.stringify(raw)}</span>;
  }
  return <span className="break-words">{String(raw)}</span>;
}

function FieldRow({ name, prov }) {
  const [open, setOpen] = useState(false);
  // `ProvenanceField` = { value, verification_status, source, normalized }
  const pf = prov && typeof prov === 'object' ? prov : {};
  const status = pf.verification_status;
  const m = status ? REC_VERIFY[status] : null;
  const src = pf.source || null;
  const hasEvidence = Boolean(src && (src.quote || src.url || src.page_id));

  return (
    <div className="border-b border-rule last:border-0">
      <button
        type="button"
        onClick={() => hasEvidence && setOpen((o) => !o)}
        className={`row flex w-full items-start gap-2.5 px-4 py-2.5 text-left ${
          hasEvidence ? 'cursor-pointer' : 'cursor-default'
        }`}
        aria-expanded={hasEvidence ? open : undefined}
      >
        <span className="mt-[3px] shrink-0" title={m?.hint || 'No judge assessment for this field'}>
          {m ? (
            <span className={toneClass(m.tone).split(' ')[0]} aria-hidden="true">
              {m.glyph}
            </span>
          ) : (
            <span className="text-rule-2" aria-hidden="true">
              ·
            </span>
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="eyebrow block">{name}</span>
          <span className="mt-0.5 block text-[12.5px] leading-snug text-ink">
            <FieldValue raw={pf.value} />
          </span>
        </span>
        {hasEvidence ? (
          <span
            className="mt-px shrink-0 text-[10px] text-muted transition-transform duration-200 ease-swift"
            style={{ transform: open ? 'rotate(90deg)' : 'none' }}
            aria-hidden="true"
          >
            ›
          </span>
        ) : null}
      </button>

      {open && hasEvidence ? (
        <div className="space-y-2 border-t border-dashed border-rule bg-warm/30 px-4 py-2.5 animate-fade-in">
          {src.quote ? (
            <blockquote className="border-l-2 border-accent/50 pl-2.5 text-[12px] italic leading-relaxed text-ink-2">
              “{truncate(src.quote, 480)}”
            </blockquote>
          ) : (
            <p className="text-[11.5px] text-warn">
              This field has a source but no verbatim quote, so it cannot be re-checked against the
              stored page.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2 pl-2.5">
            {src.url ? (
              <a href={src.url} target="_blank" rel="noreferrer noopener" className="link text-[11px]">
                {src.title || host(src.url)}
              </a>
            ) : null}
            {src.page_id ? (
              <span
                className="font-mono text-[10px] text-muted"
                title="Stored page this quote was located in"
              >
                page {String(src.page_id).slice(0, 8)}
              </span>
            ) : (
              <span className="text-[10px] text-warn" title="No stored page backing this claim">
                no stored page
              </span>
            )}
            {src.start !== null && src.start !== undefined ? (
              <span
                className="font-mono text-[10px] text-muted"
                title="Character offsets of the quote inside the stored page text"
              >
                @{src.start}–{src.end}
              </span>
            ) : null}
            {src.retrieved_at ? (
              <span className="text-[10px] text-muted">{when(src.retrieved_at)}</span>
            ) : null}
          </div>

          {pf.normalized ? (
            <p className="pl-2.5 text-[10.5px] text-muted">
              normalised for dedupe as{' '}
              <code className="font-mono text-ink-2">
                {truncate(
                  typeof pf.normalized.normalized === 'string'
                    ? pf.normalized.normalized
                    : JSON.stringify(pf.normalized.normalized),
                  80
                )}
              </code>
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function RecordView({ record }) {
  if (!record) return <Empty title="No record selected" />;

  // `RecordRow` is `{ fields: { name: ProvenanceField } }`. Older/persisted rows
  // may be a flat `{name: value}` map, so both are accepted.
  const fields =
    record.fields && typeof record.fields === 'object'
      ? Object.entries(record.fields).map(([k, v]) => [k, v])
      : Object.entries(record)
          .filter(([, v]) => v === null || typeof v !== 'object')
          .map(([k, v]) => [k, { value: v, verification_status: null, source: null }]);

  const proven = fields.filter(([, pf]) => pf?.verification_status === 'verified').length;
  // "Not proven" is everything that is not `verified` — including `unverified`
  // and `judgment_unavailable`. Counting only fields with *no* status at all
  // reported "0 unjudged" on records where most fields were in fact unproven,
  // which is precisely the false comfort this tool exists to avoid.
  const notProven = fields.length - proven;

  return (
    <>
      <div className="grid grid-cols-3 divide-x divide-rule border-b border-rule">
        <div className="px-4 py-2.5">
          <p className="eyebrow">Fields</p>
          <p className="mt-0.5 font-mono text-[15px] leading-none text-ink">{fields.length}</p>
        </div>
        <div className="px-4 py-2.5">
          <p className="eyebrow">Proven</p>
          <p
            className={`mt-0.5 font-mono text-[15px] leading-none ${
              proven ? 'text-ok' : 'text-muted'
            }`}
          >
            {proven}
          </p>
        </div>
        <div className="px-4 py-2.5">
          <p className="eyebrow" title="Not judge-verified: unverified, no judgment, throttled or conflicting">
            Not proven
          </p>
          <p
            className={`mt-0.5 font-mono text-[15px] leading-none ${
              notProven ? 'text-warn' : 'text-muted'
            }`}
          >
            {notProven}
          </p>
        </div>
      </div>

      <div>
        {fields.length ? (
          fields.map(([k, pf]) => <FieldRow key={k} name={k} prov={pf} />)
        ) : (
          <Empty
            title="Record has no fields"
            hint="Extraction produced an empty payload for this row."
          />
        )}
      </div>

      <details className="group border-t border-rule">
        <summary className="focusable cursor-pointer list-none px-4 py-2.5 text-[11.5px] text-muted transition-colors hover:text-ink">
          <span
            className="inline-block transition-transform duration-200 ease-swift group-open:rotate-90"
            aria-hidden="true"
          >
            ›
          </span>{' '}
          Raw record
        </summary>
        <div className="px-4 pb-4">
          <pre className="scroll-y max-h-72 overflow-auto rounded-sm border border-rule bg-warm/50 p-2.5 font-mono text-[11px] leading-relaxed text-ink-2">
            {JSON.stringify(record, null, 2)}
          </pre>
        </div>
      </details>
    </>
  );
}

function SourceView({ source }) {
  if (!source) return <Empty title="No source selected" />;
  const s = source;
  const url = s.url || s.final_url || s.page_url;
  // `GET /sources` rows are `{url, title, content_hash, status, error}`. A
  // source seen live on the event stream additionally carries `char_count`,
  // `page_id` and `rendered`. Both are rendered, whichever fields exist.
  const good = s.ok ?? (s.status ? s.status === 'ok' : true);

  return (
    <div className="space-y-3 px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone={good ? 'ok' : 'danger'} glyph={good ? '✓' : '✕'}>
          {good ? 'Fetched' : s.status || 'Failed'}
        </Pill>
        {s.rendered ? (
          <Pill tone="info" title="Rendered with a real browser engine">
            rendered
          </Pill>
        ) : null}
        {s.robots_allowed !== undefined ? (
          <Pill tone={s.robots_allowed ? 'ok' : 'warn'}>{s.robots_allowed ? 'robots ok' : 'robots denied'}</Pill>
        ) : null}
      </div>

      {s.title ? <p className="text-[13px] leading-snug text-ink">{s.title}</p> : null}

      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          className="link block break-all text-[12px]"
        >
          {url}
        </a>
      ) : null}

      <dl className="divide-y divide-rule border-y border-rule">
        <KeyVal k="Host" v={host(url)} />
        {s.char_count !== undefined ? (
          <KeyVal k="Stored characters" v={num(s.char_count)} mono />
        ) : null}
        {s.content_hash ? (
          <KeyVal k="Content hash" v={String(s.content_hash)} mono />
        ) : null}
        {s.page_id ? <KeyVal k="Page ID" v={String(s.page_id)} mono /> : null}
        {s.status ? <KeyVal k="Status" v={s.status} /> : null}
        {s.fetched_at || s.retrieved_at ? (
          <KeyVal k="Retrieved" v={when(s.fetched_at || s.retrieved_at)} />
        ) : null}
      </dl>

      {s.content_hash ? (
        <p className="text-[11px] leading-relaxed text-muted">
          The content hash is the page's identity in storage. Re-fetching the URL and matching this
          hash is what proves the stored evidence still reflects the live page.
        </p>
      ) : null}

      {s.error ? <ErrorNote error={{ message: s.error }} compact /> : null}
    </div>
  );
}

function DiagnosticsView({ health, loading }) {
  const rows = useMemo(() => {
    if (!health) return [];
    const out = [];
    const walk = (obj, prefix = '') => {
      Object.entries(obj || {}).forEach(([k, v]) => {
        const key = prefix ? `${prefix}.${k}` : k;
        if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, key);
        else out.push([key, v]);
      });
    };
    walk(health);
    return out;
  }, [health]);

  if (loading && !health) return <div className="p-4"><SkeletonLines n={6} /></div>;
  if (!health) return <Empty title="Health unavailable" hint="The API did not respond." />;

  return (
    <>
      <div className="border-b border-rule px-4 py-2.5">
        <p className="eyebrow">Everything the API reports</p>
        <p className="mt-1 text-[11.5px] text-muted">
          Flattened live from <code className="font-mono">GET /api/health</code>
        </p>
      </div>
      <dl className="divide-y divide-rule">
        {rows.map(([k, v]) => (
          <KeyVal key={k} k={k} v={typeof v === 'boolean' ? (v ? 'yes' : 'no') : v} mono={typeof v === 'number'} />
        ))}
      </dl>
    </>
  );
}

function EventView({ event }) {
  if (!event) return <Empty title="No event selected" />;
  const { type, stage, ...rest } = event;
  return (
    <>
      <div className="border-b border-rule px-4 py-2.5">
        <p className="eyebrow">Event type</p>
        <p className="mt-1 font-mono text-[12.5px] text-ink">{type}</p>
        {stage ? <p className="mt-0.5 font-mono text-[11px] text-muted">stage: {stage}</p> : null}
      </div>
      <div className="p-4">
        <pre className="scroll-y max-h-[60vh] overflow-auto rounded-sm border border-rule bg-warm/50 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
          {JSON.stringify(rest, null, 2)}
        </pre>
      </div>
    </>
  );
}

function RunView({ run }) {
  if (!run) return <Empty title="No run loaded" />;
  const key = run.status === 'FAILED' && run.partial ? 'FAILED+partial' : run.status;
  const meta = RUN_STATUS[key] || RUN_STATUS.QUEUED;
  return (
    <>
      <div className="flex items-center gap-1.5 border-b border-rule px-4 py-2.5">
        <Pill tone={meta.tone} glyph={meta.glyph} live={meta.live}>
          {meta.label}
        </Pill>
        {run.partial ? (
          <Pill tone="warn" glyph="◐" title="A dataset was written before the run budget ran out">
            partial saved
          </Pill>
        ) : null}
      </div>
      <dl className="divide-y divide-rule">
        <KeyVal k="Run ID" v={String(run.run_id || run.id || '').slice(0, 18)} mono />
        <KeyVal k="Stage" v={run.current_stage} />
        <KeyVal k="Progress" v={run.progress !== undefined ? `${run.progress}%` : null} mono />
        {run.dataset_id ? <KeyVal k="Dataset" v={run.dataset_id} mono /> : null}
        {run.no_yield_reason ? <KeyVal k="No yield because" v={run.no_yield_reason} /> : null}
        {run.error ? <KeyVal k="Error" v={run.error} /> : null}
      </dl>
      {run.counters ? (
        <dl className="divide-y divide-rule border-t border-rule">
          {Object.entries(run.counters)
            .filter(([, v]) => v !== 0 && v !== '' && v !== null && v !== undefined)
            .map(([k, v]) => (
              <KeyVal key={k} k={k.replace(/_/g, ' ')} v={v} mono />
            ))}
        </dl>
      ) : null}
    </>
  );
}

/* ----------------------------------------------------------------- shell */

const TITLES = {
  record: ['Record', 'Stored extraction with per-field evidence'],
  source: ['Source', 'Fetched page and its stored evidence'],
  event: ['Event', 'Raw stream event'],
  diagnostics: ['Diagnostics', 'Backend health and configuration'],
  run: ['Run', 'Execution state'],
};

export default function Inspector({ onClose }) {
  const { item } = useInspector();
  const ref = useRef(null);
  const [eyebrow, defTitle, defSub] = TITLES[item?.kind] || ['Detail', '', ''];

  useEffect(() => {
    ref.current?.scrollTo({ top: 0 });
  }, [item?.kind, item?.id]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!item) return null;

  let body = null;
  if (item.kind === 'record') body = <RecordView record={item.record} />;
  else if (item.kind === 'source') body = <SourceView source={item.source} />;
  else if (item.kind === 'event') body = <EventView event={item.event} />;
  else if (item.kind === 'diagnostics') body = <DiagnosticsView health={item.health} loading={item.loading} />;
  else if (item.kind === 'run') body = <RunView run={item.run} />;

  return (
    <aside
      ref={ref}
      aria-label="Inspector"
      className="chrome-blur scroll-y z-20 hidden w-[var(--inspector-w)] shrink-0 animate-slide-left border-l border-rule md:block"
    >
      <Head
        eyebrow={eyebrow}
        title={item.title || defTitle}
        sub={item.sub || defSub}
        onClose={onClose}
        actions={
          item.json ? <CopyButton value={JSON.stringify(item.json, null, 2)} label="JSON" /> : null
        }
      />
      {body}
    </aside>
  );
}

