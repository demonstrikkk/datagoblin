import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { REC_VERIFY, host, toneClass, truncate, when } from '../lib/format.js';
import { Empty, ErrorNote, SkeletonLines } from './ui.jsx';

/**
 * The evidence layer.
 *
 * Everything the product claims it can do comes down to one chain:
 *
 *     value → quote → stored page → source
 *
 * These components render that chain, and they are the only place it is
 * rendered. The record inspector, the dataset ledger and the review queue all
 * use them, so a claim cannot look well-evidenced in one view and unproven in
 * another.
 *
 * Two rules govern everything below.
 *
 * 1. Never overstate. A value with no quote, or a quote with no stored page, is
 *    *not* quietly upgraded to "verified" to make a panel look tidy. It says
 *    exactly which link in the chain is missing.
 * 2. Never invent. The states shown are the backend's own
 *    `verification_status` values. `GROUPED_STATUS` below only decides which
 *    *word* is used for a state that already exists; it cannot create one.
 */

/* ------------------------------------------------------------ field shape */

/**
 * Longest field value shown inline before it is clamped.
 *
 * A `description` is a paragraph and `product_photos` is a list of URLs; either
 * rendered in full pushes every other field on the record off the panel.
 */
const LONG_VALUE = 420;

/**
 * Render a stored field value readably.
 *
 * Raw extraction output is often an unformatted number — `17000000000` for a
 * funding amount. Printing that verbatim is unreadable, so large integers get
 * thousands separators plus a compact gloss. The exact value stays in the
 * title attribute, because rounding a figure someone may rely on would be its
 * own kind of lie.
 *
 * Text is clamped rather than dropped: the full value is one click away, so
 * bounding what is shown does not mean hiding what was extracted.
 */
export function FieldValue({ raw }) {
  const [open, setOpen] = useState(false);
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
  const text =
    typeof raw === 'object' ? (Array.isArray(raw) ? raw.join(', ') : JSON.stringify(raw)) : String(raw);

  if (text.length <= LONG_VALUE) return <span className="break-words">{text}</span>;
  if (open) {
    return (
      <span className="block">
        <span className="block break-words whitespace-pre-wrap">{text}</span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="mt-1 text-[11px] text-muted underline underline-offset-2 hover:text-ink"
        >
          Show less
        </button>
      </span>
    );
  }
  return (
    <span className="block">
      <span className="block break-words">{`${text.slice(0, LONG_VALUE).trimEnd()}…`}</span>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-1 text-[11px] text-muted underline underline-offset-2 hover:text-ink"
      >
        Show all {text.length.toLocaleString('en-US')} characters
      </button>
    </span>
  );
}

/**
 * Accept either shape a field arrives in and return one provenance field.
 *
 * A `ProvenanceField` is `{ value, verification_status, source, normalized }`.
 * Rows persisted before provenance was recorded are a flat `{name: value}` map,
 * and a value that is itself an object (a list of URLs, say) is not a
 * provenance wrapper. Reading `.value` off a bare object would silently return
 * undefined, which rendered as an empty cell rather than as missing evidence.
 */
export function asField(pf) {
  if (pf && typeof pf === 'object' && !Array.isArray(pf) && 'value' in pf) {
    return {
      value: pf.value,
      status: pf.verification_status ?? null,
      source: pf.source ?? null,
      normalized: pf.normalized ?? null,
    };
  }
  return { value: pf, status: null, source: null, normalized: null };
}

/** How much of a chain is actually present, so a rail can stop where it ends. */
export function evidenceDepth(source) {
  if (!source) return 0;
  let depth = 0;
  if (source.url) depth += 1;
  if (source.quote) depth += 1;
  if (source.page_id) depth += 1;
  return depth;
}

/**
 * Presentation wording for a backend status.
 *
 * The brief behind this design asks for five reader-facing words — proven,
 * judged, unresolved, disputed, empty — while the API emits seven distinct
 * states. Collapsing them would discard real information (`rate_limited` is an
 * operational condition, not a verdict), so the seven are kept intact and only
 * the headline noun is softened where the backend's word is internal jargon.
 */
export const GROUPED_STATUS = {
  verified: 'Proven',
  judgment_unavailable: 'Unresolved',
  rate_limited: 'Unresolved',
  unverified: 'Unresolved',
  conflicting: 'Disputed',
  needs_review: 'Needs review',
  rejected: 'Rejected',
};

/** Does this value have a stored page to re-check a quote against? */
export function canProve(source) {
  return Boolean(
    source &&
      source.page_id &&
      source.quote &&
      Number.isInteger(source.start) &&
      Number.isInteger(source.end)
  );
}

/* ------------------------------------------------------------- cell status */

/**
 * The status glyph shown against a single value.
 *
 * Deliberately not a checkmark-for-verified-and-nothing-else. The pipeline has
 * seven states and three of them mean "a person or a retry should look at this",
 * so a single tick would report success for values the product has not
 * established. The glyph is muted and small; the meaning is on hover and on the
 * accessible name, because a bare ✓ in a dense table is unreadable on its own.
 */
export function CellStatus({ status, size = 'md', className = '' }) {
  const meta = status ? REC_VERIFY[status] : null;
  const glyph = meta ? meta.glyph : '·';
  const tone = meta ? meta.tone : 'muted';
  const px = size === 'sm' ? 'text-[11px]' : 'text-[13px]';
  return (
    <span
      className={`${px} ${toneClass(tone).split(' ')[0]} select-none ${className}`}
      title={meta ? meta.hint : 'No verification check has run against this value'}
    >
      <span aria-hidden="true">{glyph}</span>
      <span className="sr-only">
        {meta ? `${GROUPED_STATUS[status] || meta.label}: ${meta.hint}` : 'No evidence'}
      </span>
    </span>
  );
}

/* ---------------------------------------------------------- evidence quote */

/**
 * The quote, with the value marked inside it.
 *
 * A quote is only evidence for a value if the value is actually in it, so the
 * match is attempted and its absence is reported. When the value is not found
 * the quote is still shown, unmarked, with the reason stated — because a reader
 * deciding whether to trust a field needs to see the quote that failed, not an
 * empty box.
 */
export function EvidenceQuote({ value, quote, className = '' }) {
  if (!quote) {
    return (
      <p className={`text-[11.5px] leading-relaxed text-warn ${className}`}>
        This value has a source but no verbatim quote, so it cannot be re-checked against the stored
        page.
      </p>
    );
  }

  const needle =
    value === null || value === undefined || value === '' ? '' : String(value).trim();
  const hay = String(quote);
  // Longest-first: a shortened or rounded value often appears inside a longer
  // exact form, and the first hit is the more precise span of the two.
  const at = needle.length >= 2 ? hay.toLowerCase().indexOf(needle.toLowerCase()) : -1;
  const matched = at >= 0;

  return (
    <div className={className}>
      <blockquote className="font-display text-[15px] leading-[1.45] text-ink">
        {/* The quote is always delimited, matched or not. The marks identify the
            value *within* an already-quoted sentence; dropping the quotation
            marks when the value is found would make the proven case look less
            like a citation than the unproven one. */}
        <span aria-hidden="true">“</span>
        {matched ? (
          <>
            {hay.slice(0, at)}
            <mark className="proof">{hay.slice(at, at + needle.length)}</mark>
            {hay.slice(at + needle.length)}
          </>
        ) : (
          hay
        )}
        <span aria-hidden="true">”</span>
      </blockquote>
      <p className="mt-1.5 text-[10.5px] text-muted">
        {matched ? (
          <>The value appears verbatim in the text its evidence was taken from.</>
        ) : needle ? (
          <>
            The stored text does not contain <code className="font-mono text-ink-2">{truncate(needle, 60)}</code>.
            The value was retained anyway.
          </>
        ) : (
          'No value was recorded for this field, so the quote cannot support one.'
        )}
      </p>
    </div>
  );
}

/* ---------------------------------------------------------- stored page */

/** How much surrounding text to show either side of a proof hit. */
const PROOF_CONTEXT = 300;

/**
 * Slice a quote out of a stored page by its recorded offsets.
 *
 * The offsets were computed in Python, where every string index counts a
 * Unicode *code point*. JavaScript counts UTF-16 *code units*, so a single
 * character outside the Basic Multilingual Plane — an emoji, some CJK — shifts
 * every later position by one and the wrong span gets highlighted. Slicing an
 * expanded code-point array keeps the two in step.
 */
export function resolveProof(markdown, start, end) {
  const chars = [...(markdown || '')];
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > chars.length) {
    return null;
  }
  return {
    before: chars.slice(Math.max(0, start - PROOF_CONTEXT), start).join(''),
    hit: chars.slice(start, end).join(''),
    after: chars.slice(end, Math.min(chars.length, end + PROOF_CONTEXT)).join(''),
    total: chars.length,
  };
}

/**
 * The stored page, with the cited sentence highlighted where it actually sits.
 *
 * Level three of the evidence drawer: the reader has a value, they have the
 * quote, and this shows the page the quote was cut from.
 *
 * The offsets are verified against the page before anything is highlighted, and
 * the page is checked against the quote. A proof view that highlights
 * plausible-looking text it cannot confirm is worse than no proof view at all —
 * it manufactures the exact reassurance the product exists to avoid.
 */
export function SourceViewer({ source, className = '' }) {
  const [state, setState] = useState({ phase: 'loading' });

  useEffect(() => {
    let alive = true;
    setState({ phase: 'loading' });
    api
      .page(source.page_id)
      .then((payload) => {
        if (!alive) return;
        // api.request already unwraps the envelope's `data`, so the payload IS
        // the page. Reading `payload.data` here yields undefined, which makes
        // every proof view claim its offsets fall outside an empty page.
        const page = payload || {};
        const markdown = page.markdown || '';
        const proof = resolveProof(markdown, source.start, source.end);
        if (!proof) setState({ phase: 'bad-offsets', page });
        else if (source.quote && proof.hit !== source.quote) {
          setState({ phase: 'mismatch', proof, page });
        } else setState({ phase: 'ok', proof, page });
      })
      .catch((err) => {
        if (alive) setState({ phase: 'error', message: err?.message || 'Could not load the page.' });
      });
    return () => {
      alive = false;
    };
  }, [source.page_id, source.start, source.end, source.quote]);

  if (state.phase === 'loading') return <SkeletonLines lines={3} />;
  if (state.phase === 'error') {
    return <ErrorNote title="Stored page unavailable">{state.message}</ErrorNote>;
  }
  if (state.phase === 'bad-offsets') {
    return (
      <p className="text-[11.5px] leading-relaxed text-warn">
        This quote carries offsets that do not fall inside the stored page, so it cannot be shown in
        context.
      </p>
    );
  }
  if (state.phase === 'mismatch') {
    return (
      <div className="space-y-1.5">
        <p className="text-[11.5px] leading-relaxed text-warn">
          The stored page no longer matches this quote: the recorded offsets select different text.
          The page may have been re-crawled since this value was verified.
        </p>
        <p className="text-[11px] text-muted">
          offsets select{' '}
          <code className="font-mono text-ink-2">“{truncate(state.proof.hit, 160)}”</code>
        </p>
      </div>
    );
  }

  const { proof, page } = state;
  return (
    <div className={`space-y-2 ${className}`}>
      <p className="text-[10.5px] uppercase tracking-[0.14em] text-muted">
        {page?.url ? host(page.url) : 'stored page'} · characters {source.start}–{source.end} of{' '}
        {proof.total.toLocaleString('en-US')}
      </p>
      <blockquote className="scroll-y max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-l border-accent/45 pl-3 font-display text-[13.5px] leading-[1.5] text-ink-2">
        {proof.before ? <span className="text-muted">{proof.before}</span> : null}
        <mark className="proof">{proof.hit}</mark>
        <span className="text-muted">{proof.after}</span>
      </blockquote>
      <p className="text-[10.5px] text-muted">
        Verbatim match against the page stored at run time.
        {page?.retrieved_at ? ` Retrieved ${when(page.retrieved_at)}.` : null}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------ the rail */

/**
 * Dot colours, written out rather than interpolated.
 *
 * `bg-${tone}` looks equivalent and is not: Tailwind scans source text for
 * complete class names, so a class assembled at runtime is never generated and
 * every rail dot renders with no background at all. A lookup table keeps the
 * names literal.
 */
const DOT = {
  ink: 'bg-ink',
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  muted: 'bg-rule-2',
};

function RailNode({ label, children, missing, missingWhy, tone = 'ink' }) {
  return (
    <div className="relative pl-5">
      {/* The spine. Each node hangs off the same rule, so the chain reads as
          one object rather than four stacked boxes. */}
      <span
        aria-hidden="true"
        className="absolute left-[3px] top-[7px] h-[calc(100%+18px)] w-px bg-rule-2 last:h-0"
      />
      <span
        aria-hidden="true"
        className={`absolute left-0 top-[5px] h-[7px] w-[7px] rounded-full border ${
          missing ? 'border-rule-2 bg-paper' : `border-transparent ${DOT[tone] || DOT.ink}`
        }`}
      />
      <p className="eyebrow">{label}</p>
      {missing ? (
        <p className="mt-0.5 text-[11.5px] leading-snug text-muted">{missingWhy}</p>
      ) : (
        <div className="mt-0.5 min-w-0">{children}</div>
      )}
    </div>
  );
}

/**
 * The Evidence Rail — value, quote, stored page, source.
 *
 * This is the product's signature, and it is the one view that answers the
 * question the rest of the interface exists to set up: *why should I believe
 * this?*
 *
 * It is progressive by construction. Each link is rendered from real data or
 * reported as missing, so a reader can see at a glance how far the chain
 * actually reaches — and a field whose value was retained without offsets is
 * visibly shorter than one that resolves into a stored page.
 */
export function ProvenanceRail({ name, prov, showPage = false, className = '' }) {
  const { value, status, source } = asField(prov);
  const meta = status ? REC_VERIFY[status] : null;
  const empty = value === null || value === undefined || value === '';
  // A node's dot carries the state, so the shape of the chain is legible before
  // any of it is read.
  const tone =
    status === 'verified'
      ? 'ok'
      : status === 'conflicting' || status === 'rejected'
        ? 'danger'
        : empty
          ? 'muted'
          : 'ink';

  return (
    <div className={`text-[12.5px] ${className}`}>
      <RailNode
        label={name || 'value'}
        missing={empty}
        missingWhy="No value was extracted for this field."
        tone={tone}
      >
        <div className="flex items-start gap-2">
          <span className="min-w-0 flex-1 text-[13px] leading-snug text-ink">
            <FieldValue raw={value} />
          </span>
          <CellStatus status={status} />
        </div>
        {meta ? <p className="mt-1 text-[11px] leading-snug text-muted">{meta.hint}</p> : null}
      </RailNode>

      <RailNode
        label="evidence"
        missing={!source?.quote}
        missingWhy={
          source
            ? 'A source was recorded without a verbatim quote, so nothing can be re-checked.'
            : 'No evidence was recorded for this value.'
        }
      >
        <EvidenceQuote value={value} quote={source.quote} />
      </RailNode>

      <RailNode
        label="stored page"
        missing={!source?.page_id}
        missingWhy={
          source?.quote
            ? 'The quote was captured but no page was stored to resolve it against.'
            : 'Nothing was stored, so there is nothing to show.'
        }
      >
        <div className="space-y-1.5">
          {source.title || source.url ? (
            <p className="text-[12px] leading-snug text-ink-2">{source.title || host(source.url)}</p>
          ) : null}
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-muted">
            {source.url ? (
              <a
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
                className="link font-mono text-[10.5px]"
              >
                {host(source.url)} ↗
              </a>
            ) : null}
            {source.page_id ? (
              <span className="font-mono" title="Stored page this quote was located in">
                page {String(source.page_id).slice(0, 8)}
              </span>
            ) : null}
            {Number.isInteger(source.start) ? (
              <span className="font-mono" title="Character offsets of the quote in the stored page">
                @{source.start}–{source.end}
              </span>
            ) : null}
            {source.retrieved_at ? <span>{when(source.retrieved_at)}</span> : null}
          </div>
          {showPage && canProve(source) ? <SourceViewer source={source} /> : null}
        </div>
      </RailNode>
    </div>
  );
}

/** Level 1: the glance. One line that says what this value rests on. */
export function EvidenceSummary({ prov, className = '' }) {
  const { value, status, source } = asField(prov);
  const meta = status ? REC_VERIFY[status] : null;
  const empty = value === null || value === undefined || value === '';

  if (empty && !source) {
    return <p className={`text-[11.5px] text-muted ${className}`}>Nothing I can defend yet.</p>;
  }
  if (!source) {
    return (
      <span className={`inline-flex items-center gap-1.5 ${className}`}>
        <CellStatus status={status} size="sm" />
        <span className="text-[11.5px] text-muted">
          {meta ? GROUPED_STATUS[status] || meta.label : 'No evidence'} · no source
        </span>
      </span>
    );
  }
  return (
    <span className={`inline-flex min-w-0 items-center gap-1.5 ${className}`}>
      <CellStatus status={status} size="sm" />
      <span className="truncate text-[11.5px] text-ink-2">
        {source.title || host(source.url)}
      </span>
      {canProve(source) ? (
        <span className="shrink-0 text-[10px] text-ok" title="Resolves into a stored page at the cited offsets">
          proof
        </span>
      ) : (
        <span className="shrink-0 text-[10px] text-warn" title="No stored page behind this quote">
          unproven
        </span>
      )}
    </span>
  );
}

/* ------------------------------------------------------------ empty voice */

/**
 * The product's own voice for "there is nothing here".
 *
 * These read as a person reporting an outcome rather than a UI admitting an
 * absence, but they are paired with the fact every one of them states — pages
 * inspected, reason, what would change it. The wit is decoration on a truthful
 * sentence, never a substitute for one.
 */
export const EMPTY_STATE = {
  noDatasets: {
    icon: '⌸',
    title: 'The shelves are empty.',
    hint: 'Nothing has been hunted yet. Give the goblin something to collect and it will come back with pages it can point at.',
  },
  noEvidence: {
    icon: '⌸',
    title: 'Nothing I can defend yet.',
    hint: 'A value was kept, but no quote and stored page sit behind it — so it is shown as unresolved rather than proven.',
  },
  noConflicts: {
    icon: '≡',
    title: 'Nothing is fighting for the same cell.',
    hint: 'Every competing value either agreed or was resolved. Disagreements appear here the moment a second source contradicts the first.',
  },
  noSources: {
    icon: '⌖',
    title: 'No trail left behind.',
    hint: 'No page has been fetched yet. The trail appears as soon as a hunt fetches something it can cite.',
  },
  nothingWorthCarrying: {
    icon: '⌸',
    title: 'Nothing worth carrying back.',
    hint: 'The pages that were inspected did not contain enough evidence for the fields that were asked for.',
  },
  partialCatch: {
    icon: '◐',
    title: 'I brought back what I could.',
    hint: 'The run stopped before it finished, so this dataset is incomplete. What survived is stored and the gap is recorded.',
  },
  huntStopped: {
    icon: '◐',
    title: 'The hunt stopped early.',
    hint: 'The run ended before the plan finished. Anything already proven is kept; the rest is not guessed at.',
  },
};

export function VoiceEmpty({ state, action, className = '' }) {
  const c = EMPTY_STATE[state];
  if (!c) return <Empty title="Nothing here" className={className} />;
  return <Empty icon={c.icon} title={c.title} hint={c.hint} action={action} className={className} />
}
