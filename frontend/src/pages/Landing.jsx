/*
  The direction contract for this surface lives in two places: the readable
  version below, and an HTML comment as the first child of <body> in
  index.html, which is the copy that survives the production build and can be
  grepped out of dist/ to audit.

  THESIS: the record is the hero. The page shows its evidence table before it
  shows its argument, because the visitor is a data engineer who has already
  been burned defending a number a model produced, and an assertion they can
  audit persuades where one they must trust does not. It refuses the
  headline-and-three-feature-cards template and the hero-metric template: the
  first viewport is a real dataset from a real run with a live proof rail.

  OWN-WORLD: inherited from the operator console, not reinvented. Warm paper and
  ink, one bronze accent, judge purple reserved for judge decisions. Instrument
  Serif for anything a human wrote, DM Mono for anything machine-derived —
  values, column headers, offsets, hashes and statuses are mono, names are serif.
  The structural motif is the citation dossier: hairline-ruled ledgers, a record
  and its chain of custody in one bordered object, and the signature proof
  highlight — a value sitting inside the sentence that supports it.

  STORY: any cell opens onto the exact stored text; then a quote that resolves
  perfectly turns out to be the wrong column; then the pipeline that produced it
  is drawn end to end. Belief: provenance is a system, not a promise. Action: run
  it and check the rail themselves.

  FIRST VIEWPORT: left, the claim in serif over a lede and two actions; right and
  dominant, the dossier — a recorded prompt, seven rows of a real run, and a
  proof rail that opens under any value.

  FORM: candidate 7 of the grounded list — "the record is the hero" dossier.
  Seed key 32f41c97.

  PROSE BUDGET: the page is diagrams first. Every section that could be drawn
  is drawn — the pipeline, the meaning gap, the confidence gate, the status
  vocabulary. Paragraphs are used only where an argument cannot be a picture,
  and then never more than two.

  FINISH: unreviewed and undocumented is unfinished; this build ends with the
  finish review, the verdict, and DESIGN.md.
*/

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AmbientField from '../components/AmbientField.jsx';
import fixture from '../landing/evidence.fixture.json';
import Reel from '../landing/Reel.jsx';
import { useNearViewport, useStoredPage } from '../landing/storedPage.js';
import '../landing/Landing.css';

const { _provenance: P, rows: ROWS } = fixture;

/* --- the honest data, derived from the fixture rather than typed twice ----- */
const CITED = P.status_tally.verified + P.status_tally.unverified;
const VERIFIED_PCT = (P.status_tally.verified / CITED) * 100;

/* The sharpest thing this page knows, located in the stored text rather than
   asserted: this run was asked for a valuation, and the quote it cites sits
   under a revenue column header. Both offsets are found in the stored text at
   runtime, so the claim cannot drift away from the text it describes. */
const REV_HEADER = 'Revenue (in **₹ crore** )';
const RELIANCE_REVENUE = '997,795';

/* ==========================================================================
   Small pieces
   ========================================================================== */

function useReveal() {
  const ref = useRef(null);
  useEffect(() => {
    const root = ref.current;
    if (!root) return undefined;
    const targets = root.querySelectorAll('.lg-reveal:not([data-shown])');
    if (!targets.length) return undefined;

    // Only opt into the hidden-then-revealed state when the observer can
    // actually run. Anything else leaves the page plainly visible.
    const still =
      !('IntersectionObserver' in window) ||
      matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (still) {
      targets.forEach((t) => t.setAttribute('data-shown', 'true'));
      return undefined;
    }

    root.setAttribute('data-anim', 'on');
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.setAttribute('data-shown', 'true');
            io.unobserve(e.target);
          }
        });
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.04 },
    );
    targets.forEach((t) => io.observe(t));

    // A safety net: anything still hidden after a few seconds — because the
    // page was printed, captured, or the observer never fired for it — is
    // shown. A reveal is a treat, not a gate.
    const failsafe = setTimeout(() => {
      root
        .querySelectorAll('.lg-reveal:not([data-shown])')
        .forEach((t) => t.setAttribute('data-shown', 'true'));
    }, 4000);

    return () => {
      clearTimeout(failsafe);
      io.disconnect();
    };
  }, []);
  return ref;
}

function Reveal({ delay = 0, as: Tag = 'div', className = '', children, ...rest }) {
  return (
    <Tag
      className={`lg-reveal ${className}`.trim()}
      style={delay ? { '--lg-delay': `${delay}ms` } : undefined}
      {...rest}
    >
      {children}
    </Tag>
  );
}

function Status({ tone = 'muted', live = false, children }) {
  return (
    <span className="lg-status" data-tone={tone}>
      {live ? <span className="lg-live" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/* A copy control that confirms itself, because a copy button that gives no
   signal is worse than no button. */
function CopyCmd({ text }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = useCallback(async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }, [text]);

  return (
    <button type="button" className="lg-copy" data-copied={copied} onClick={copy}>
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

/* ==========================================================================
   The evidence rail — the product's signature moment, so the most carefully
   made object on the page. Everything it shows is read out of the fixture.
   ========================================================================== */

const WINDOW = 190;

function StoredText({ text, cite, head }) {
  if (!text) return null;
  const start = cite ? cite[0] : 0;
  const end = cite ? cite[1] : 0;
  const from = Math.max(0, start - WINDOW);
  const to = Math.min(text.length, end + WINDOW);

  return (
    <p className="lg-stored-text">
      {from > 0 ? '…' : ''}
      <span className="lg-cite-ctx">{text.slice(from, start)}</span>
      {head ? <span className="lg-cite-head">{head}</span> : null}
      <span className="lg-cite">{text.slice(start, end)}</span>
      <span className="lg-cite-ctx">{text.slice(end, to)}</span>
      {to < text.length ? '…' : ''}
    </p>
  );
}

function Scrubber({ text, quote, citeStart, onScrub }) {
  const max = Math.max(0, text.length - quote.length);
  const [pos, setPos] = useState(citeStart);
  const matched = pos === citeStart;

  useEffect(() => {
    setPos(citeStart);
  }, [citeStart]);

  return (
    <div className="lg-scrub">
      <div className="lg-scrub-top">
        <span className="lg-tag">Drag to hunt the quote in the stored text</span>
        <span className="lg-scrub-state" data-matched={matched}>
          {matched ? 'Resolved · offsets match' : `${pos}–${pos + quote.length} · no match`}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={max}
        value={pos}
        aria-label="Character offset of the cited span within the stored page"
        aria-valuetext={`${pos} to ${pos + quote.length} characters`}
        onChange={(e) => {
          const v = Number(e.target.value);
          setPos(v);
          onScrub?.(v);
        }}
      />
      <span className="lg-fine">
        {matched
          ? 'The span below is the exact range this field cites. Nothing here is an approximation.'
          : 'The cited length is held; the start moves. The sentence will not be found, because a citation that always resolves is not a citation.'}
      </span>
    </div>
  );
}

function EvidenceRail({ cell }) {
  const { text: md, error } = useStoredPage();
  const cite = cell.offsets;
  const quote = cell.quote || '';
  const [scrubPos, setScrubPos] = useState(null);

  useEffect(() => {
    setScrubPos(null);
  }, [cell.key]);

  const shown = useMemo(() => {
    if (!md || !cite) return { text: null, cite };
    if (scrubPos == null) return { text: md, cite };
    return { text: md, cite: [scrubPos, scrubPos + quote.length] };
  }, [md, scrubPos, cite, quote]);

  return (
    <aside className="lg-rail" aria-label="Evidence for the selected field">
      <div className="lg-rail-head">
        <span className="lg-tag">{cell.field}</span>
        <span className="lg-rail-title">{cell.value == null ? 'No value' : cell.value}</span>
        <span className="lg-fine" style={{ margin: 0 }}>
          {cell.value == null
            ? 'Nothing on the page supported it, so it stayed null.'
            : 'Every character of this value is quoted from stored page text.'}
        </span>
      </div>

      <div className="lg-rail-scroll">
        <dl className="lg-kv">
          <dt>Status</dt>
          <dd>
            <Status tone={cell.status === 'verified' ? 'ok' : 'muted'}>{cell.status}</Status>
          </dd>
          <dt>Quote</dt>
          <dd>{quote ? `“${quote}”` : '— none —'}</dd>
          <dt>Offsets</dt>
          <dd>{cite ? `${cite[0]} – ${cite[1]}` : '—'}</dd>
          <dt>Page</dt>
          <dd>
            <a href={P.page_url} target="_blank" rel="noreferrer noopener">
              {P.page_url.replace('https://', '')}
            </a>
          </dd>
          <dt>Chars</dt>
          <dd>{P.markdown_chars.toLocaleString('en-US')}</dd>
          <dt>Hash</dt>
          <dd>{P.content_hash.slice(0, 24)}…</dd>
        </dl>

        <div className="lg-stored">
          <div className="lg-stored-label">
            <span className="lg-tag">Stored page text</span>
            <Status tone={cite ? 'judge' : 'muted'}>
              {cite ? 'quote resolves' : 'no citation'}
            </Status>
          </div>

          {error ? (
            <p className="lg-fine" style={{ margin: 0 }}>
              The stored page text could not be loaded ({error}). The offsets above are from
              the fixture and are unaffected; only the quoted text is missing.
            </p>
          ) : md ? (
            <>
              <StoredText text={shown.text} cite={shown.cite} />
              {cite ? (
                <Scrubber
                  text={md}
                  quote={quote}
                  citeStart={cite[0]}
                  onScrub={(v) => setScrubPos(v)}
                />
              ) : null}
            </>
          ) : (
            /* Loading is the console's own dither, not a grey block: the page
               should never show a flat rectangle where evidence should be. */
            <div className="dither lg-stored-text" style={{ minHeight: 96 }} aria-busy="true">
              <span className="sr-only">Loading stored page text</span>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}

/* ==========================================================================
   The record
   ========================================================================== */

function Dossier() {
  const [sel, setSel] = useState(null);
  const touched = useRef(false);

  const cells = useMemo(() => {
    const out = [];
    ROWS.forEach((row, i) => {
      out.push({
        key: `c${i}`,
        field: 'company_name',
        value: row.company.value,
        status: row.company.status,
        quote: row.company.quote || '',
        offsets: row.company.offsets,
      });
      out.push({
        key: `v${i}`,
        field: 'valuation',
        value: row.valuation.value,
        status: row.valuation.status,
        quote: row.valuation.quote || '',
        offsets: row.valuation.offsets,
      });
    });
    return out;
  }, []);

  const active = sel ? cells.find((c) => c.key === sel) : null;

  /* The page demonstrates its own mechanism instead of waiting to be told.
     One shot, on the first verified value, and never over a reader who has
     already touched the table or asked for less motion. */
  useEffect(() => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return undefined;
    const t = setTimeout(() => {
      if (!touched.current) setSel('v0');
    }, 1600);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!sel) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setSel(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sel]);

  const pick = (key) => {
    touched.current = true;
    setSel((cur) => (cur === key ? null : key));
  };

  return (
    <div className="lg-dossier">
      <div className="lg-dossier-head">
        <Status tone="ok" live>
          {P.status}
        </Status>
        <span className="lg-prompt" title={P.prompt}>
          {P.prompt}
          <span className="lg-caret" aria-hidden="true" />
        </span>
        <span className="lg-tag">{P.run_id.slice(0, 8)}</span>
      </div>

      <div className="lg-dossier-body" data-rail={active ? 'open' : 'closed'}>
        <div>
          <table className="lg-table">
            <caption className="sr-only">
              {P.records_on_disk} records from run {P.run_id}. Select any value to see the
              stored text it was extracted from.
            </caption>
            <thead>
              <tr>
                <th scope="col">Company</th>
                <th scope="col" className="num">
                  Value
                </th>
                <th scope="col" className="num lg-th-cite">
                  Cited at
                </th>
              </tr>
            </thead>
            <tbody>
              {ROWS.map((row, i) => {
                const nameCell = cells[i * 2];
                const valCell = cells[i * 2 + 1];
                return (
                  <tr key={row.company.value}>
                    <td>
                      <button
                        type="button"
                        className="lg-cell"
                        aria-pressed={sel === nameCell.key}
                        onClick={() => pick(nameCell.key)}
                      >
                        <span className="lg-cell-name">{row.company.value}</span>
                      </button>
                    </td>
                    <td className="num">
                      <button
                        type="button"
                        className="lg-cell num"
                        aria-pressed={sel === valCell.key}
                        onClick={() => pick(valCell.key)}
                      >
                        {row.valuation.value == null ? (
                          <span className="lg-cell-null">null</span>
                        ) : (
                          <span className="lg-cell-val">
                            <span
                              className="lg-verdict"
                              data-tone={row.valuation.status === 'verified' ? 'ok' : 'muted'}
                              aria-hidden="true"
                            />
                            {row.valuation.value}
                          </span>
                        )}
                      </button>
                    </td>
                    <td className="num lg-td-cite">
                      {row.valuation.offsets ? (
                        <span className="lg-rowsub lg-rowsub-inline">
                          {row.valuation.offsets[0]}–{row.valuation.offsets[1]}
                        </span>
                      ) : (
                        <span className="lg-rowsub lg-rowsub-inline lg-rowsub-none">uncited</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="lg-kv lg-kv-foot" style={{ borderBottom: 0 }}>
            <dt>Records</dt>
            <dd>{P.records_on_disk}</dd>
            <dt>Fields</dt>
            <dd>
              {P.status_tally.verified} verified · {P.status_tally.unverified} unverified
            </dd>
            <dt>Nulls kept</dt>
            <dd>{P.records_with_null_valuation} records kept a null valuation</dd>
          </div>
        </div>

        {active ? (
          /* Keyed on the cell so the rail re-enters on every selection: the
             proof highlight is a moment, and it should land each time. */
          <div className="lg-rail-slot" key={active.key}>
            <EvidenceRail cell={active} />
          </div>
        ) : null}
      </div>

      {!active ? (
        <p className="lg-dossier-hint">
          Select any value to open its evidence. {ROWS.length} of these rows are shown from{' '}
          {P.records_on_disk}.
        </p>
      ) : null}
    </div>
  );
}

/* The run's own bookkeeping, drawn as an instrument rather than a stat row:
   the proportion strip is the point, because the honest answer to "how much of
   this run is trustworthy" is a bar with a large empty half. */
function Receipt() {
  const items = [
    { k: 'Records', v: P.records_on_disk },
    { k: 'Fields', v: P.fields_total },
    { k: 'Pages stored', v: `${P.pages_stored}/${P.pages_attempted}` },
    { k: 'Credits', v: P.credits_used },
    { k: 'Nulls kept', v: P.records_with_null_valuation },
  ];

  return (
    <div className="lg-receipt">
      <div className="lg-receipt-bar" aria-hidden="true">
        <span style={{ width: `${VERIFIED_PCT}%`, background: 'rgb(var(--ok))' }} />
        <span style={{ width: `${100 - VERIFIED_PCT}%`, background: 'rgb(var(--rule-2))' }} />
      </div>
      <div className="lg-receipt-key">
        <Status tone="ok">{P.status_tally.verified} verified</Status>
        <Status tone="muted">{P.status_tally.unverified} unverified</Status>
        <span className="lg-tag">{VERIFIED_PCT.toFixed(1)}% defensible</span>
      </div>
      <dl className="lg-receipt-grid">
        {items.map((i) => (
          <div key={i.k}>
            <dt>{i.k}</dt>
            <dd>{i.v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/* ==========================================================================
   The recorded explainer. Its closing line is this page's own rule, so it
   sits immediately under the record rather than at the end as an appendix.
   ========================================================================== */

function Explainer() {
  return (
    <section className="lg-section" id="explainer">
      <div className="lg-wrap">
        <div className="lg-split">
          <Reveal>
            <h2 className="lg-h2" style={{ fontSize: 'clamp(28px, 3.2vw, 42px)', margin: 0 }}>
              Someone else already drew it.
            </h2>
            <p className="lg-body" style={{ marginTop: 18 }}>
              A ten-minute whiteboard walkthrough of the extraction engine — the
              hallucination problem, the five stages, and offset verification. It ends on the
              same line this page opened with.
            </p>
            <p className="lg-fine">
              Recorded, not animated by us: 1280×720 at 24fps, cut to {clockShort(640.6)} with
              the audio track removed for size. The chapter marks are scene changes detected
              in the recording, not hand-placed.
            </p>
          </Reveal>

          <Reveal delay={80}>
            <Reel />
          </Reveal>
        </div>
      </div>
    </section>
  );
}

function clockShort(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

/* ==========================================================================
   The pipeline, drawn. This is the page's central diagram and the reason a
   reader trusts the record above it: it shows the order of operations, and the
   Store step that everything else depends on.

   Colour is the actor, and the legend is the only place the mapping is
   explained — the same colour appears nowhere else on the page.
   ========================================================================== */

const PHASES = [
  {
    name: 'Plan',
    who: 'LLM',
    steps: ['prompt → goal, fields, queries', 'required capped at 2', 'page budget bounded'],
  },
  {
    name: 'Find',
    who: 'Jev · Python',
    steps: ['Tavily search', 'robots.txt gate', 'Jev-A screens', 'NO ⇒ never fetched'],
  },
  {
    name: 'Store',
    who: 'Python',
    hero: true,
    steps: ['4 workers, BFS depth ≤ 2', 'markdown + raw_html', 'hash · parent · depth', 'stored BEFORE extraction'],
  },
  {
    name: 'Prove',
    who: 'LLM · Jev',
    steps: ['page_evidence_text()', 'extraction, concurrent', 'quote-substring gate', 'Jev-B verify · Jev-C merge'],
  },
  {
    name: 'Dataset',
    who: 'Postgres',
    steps: ['records + provenance', 'atomic finalize', 'CSV · JSON · JSONL · MD'],
  },
];

const ROLES = [
  { who: 'LLM', verb: 'understands' },
  { who: 'Jev', verb: 'judges' },
  { who: 'Python', verb: 'executes' },
  { who: 'Postgres', verb: 'remembers' },
];

function Pipeline() {
  return (
    <section className="lg-section" id="pipeline">
      <div className="lg-wrap">
        <div className="lg-split">
          <Reveal>
            <h2 className="lg-h2" style={{ fontSize: 'clamp(28px, 3.2vw, 42px)', margin: 0 }}>
              The order is the product.
            </h2>
            <p className="lg-body" style={{ marginTop: 18 }}>
              Most pipelines fetch a page, reduce it in memory, extract from it, and throw it
              away. Every quote then points at <span className="lg-mono">start</span>/
              <span className="lg-mono">end</span> offsets into text that no longer exists, so
              nothing can be re-checked.
            </p>
            <p className="lg-body">
              DataGoblin stores the page <em>before</em> anything reads it. That one ordering
              choice is what turns a quote from a decoration into evidence.
            </p>
          </Reveal>

          <Reveal delay={70}>
            <ul className="lg-roles">
              {ROLES.map((r) => (
                <li key={r.who} data-who={r.who}>
                  <span className="lg-role-dot" aria-hidden="true" />
                  <span className="lg-mono">{r.who}</span>
                  <span className="lg-role-verb">{r.verb}</span>
                </li>
              ))}
            </ul>
          </Reveal>
        </div>

        <Reveal delay={110}>
          <ol className="lg-flow">
            {PHASES.map((ph) => (
              <li className="lg-phase" key={ph.name} data-hero={ph.hero ? 'true' : undefined}>
                <div className="lg-phase-head">
                  <span className="lg-phase-n">{ph.name}</span>
                  <span className="lg-phase-who" data-who={ph.who}>
                    {ph.who}
                  </span>
                </div>
                <ul className="lg-phase-steps">
                  {ph.steps.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </Reveal>
      </div>
    </section>
  );
}

/* ==========================================================================
   The meaning gap. The one place a picture carries an argument prose cannot:
   a perfectly resolving citation, and the column header that decides what it
   means, with the distance between them measured.
   ========================================================================== */

function GapDiagram({ gap }) {
  return (
    <div className="lg-gap">
      <div className="lg-gap-row">
        <span className="lg-gap-label" data-kind="head">
          Column header
        </span>
        <span className="lg-gap-mark" data-kind="head">
          Revenue (in ₹ crore)
        </span>
        <span className="lg-gap-at lg-mono">char {gap.headerAt}</span>
      </div>

      <div className="lg-gap-measure">
        <span className="lg-gap-stem" aria-hidden="true" />
        <span className="lg-gap-count lg-mono">{gap.gap} characters</span>
      </div>

      <div className="lg-gap-row">
        <span className="lg-gap-label" data-kind="value">
          Cited value
        </span>
        <span className="lg-gap-mark" data-kind="value">
          {RELIANCE_REVENUE}
        </span>
        <span className="lg-gap-at lg-mono">char {gap.revenueAt}</span>
      </div>

      <div className="lg-gap-verdict">
        <Status tone="ok">quote resolves</Status>
        <span className="lg-gap-x" aria-hidden="true">
          but it is not a valuation
        </span>
      </div>
    </div>
  );
}

function Sufficiency() {
  const [ref, near] = useNearViewport();
  const { text: md, error } = useStoredPage(near);

  const found = useMemo(() => {
    if (!md) return null;
    const headerAt = md.indexOf(REV_HEADER);
    const revenueAt = md.indexOf(RELIANCE_REVENUE);
    if (headerAt < 0 || revenueAt <= headerAt) return null;
    return {
      headerAt,
      revenueAt,
      gap: revenueAt - headerAt,
      snippet: md.slice(headerAt, revenueAt + 120),
    };
  }, [md]);

  return (
    <section className="lg-section" id="sufficiency">
      <div className="lg-wrap">
        <div className="lg-split">
          <Reveal>
            <h2 className="lg-h2" style={{ fontSize: 'clamp(28px, 3.2vw, 42px)', margin: 0 }}>
              A citation proves the string was there. Not what it meant.
            </h2>
            <p className="lg-body" style={{ marginTop: 18 }}>
              This run was asked for companies with a <em>valuation</em> above ₹100 crore. The
              value below is verified — quote in the text, offsets resolved, judge agreed.
            </p>
            <p className="lg-body">
              It is a revenue figure. DataGoblin stores the page text, so the header that gives
              the number its meaning is right there. It will not close that gap for you; it
              makes the gap visible.
            </p>
          </Reveal>

          <Reveal delay={80}>
            <div className="lg-panel" ref={ref}>
              {error ? (
                <p className="lg-fine">Stored page text unavailable ({error}).</p>
              ) : found ? (
                <>
                  <div className="lg-panel-head">
                    <span className="lg-tag">Reliance Industries Limited</span>
                    <Status tone="warn">verified · wrong column</Status>
                  </div>
                  <GapDiagram gap={found} />
                  <p className="lg-stored-text lg-stored-text-sm">
                    <span className="lg-cite-head">{found.snippet.slice(0, REV_HEADER.length)}</span>
                    <span className="lg-cite-ctx">
                      {found.snippet.slice(REV_HEADER.length, found.snippet.indexOf(RELIANCE_REVENUE))}
                    </span>
                    <span className="lg-cite">{RELIANCE_REVENUE}</span>
                  </p>
                </>
              ) : (
                <div className="dither lg-stored-text" style={{ minHeight: 120 }} aria-busy="true">
                  <span className="sr-only">Loading stored page text</span>
                </div>
              )}
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

/* ==========================================================================
   The vocabulary, as a gate diagram plus a status grid. The 0.4–0.6 band is
   the whole point of this section, so it is drawn as the gate it is.
   ========================================================================== */

const STATUSES = [
  {
    tone: 'ok',
    term: 'verified',
    gloss: 'Quote found, offsets resolved, judge supported it.',
  },
  {
    tone: 'muted',
    term: 'unverified',
    gloss: 'No supporting quote — so the value was nulled, not filled.',
  },
  {
    tone: 'judge',
    term: 'judgment_unavailable',
    gloss: 'Quote is real, but no judge ruled. Deliberately not verified.',
  },
  {
    tone: 'warn',
    term: 'rate_limited',
    gloss: 'The judge asked to wait. Deferred, and says so.',
  },
  {
    tone: 'danger',
    term: 'conflicting',
    gloss: 'Two sources disagreed. The rival value is preserved.',
  },
];

function Gate() {
  return (
    <div className="lg-gate">
      <div className="lg-gate-bar" aria-hidden="true">
        <span data-zone="no" />
        <span data-zone="maybe" />
        <span data-zone="yes" />
      </div>
      <div className="lg-gate-scale lg-mono">
        <span>0.0</span>
        <span>0.4</span>
        <span>0.6</span>
        <span>1.0</span>
      </div>
      <div className="lg-gate-labels">
        <span data-zone="no">
          NOT_SUPPORTED
          <em>value nulled</em>
        </span>
        <span data-zone="maybe">
          UNCERTAIN
          <em>cannot tell</em>
        </span>
        <span data-zone="yes">
          SUPPORTED
          <em>verified</em>
        </span>
      </div>
      <p className="lg-fine">
        The judge returns probabilities; Python applies the threshold. The middle band is a
        real “I cannot tell” — it never resolves upward to verified. No judge reachable at all
        gives <span className="lg-mono">judgment_unavailable</span>, which is also not
        verified: the quote is on the page, but nothing ruled on it.
      </p>
    </div>
  );
}

function Vocabulary() {
  return (
    <section className="lg-section" id="vocabulary">
      <div className="lg-wrap">
        <div className="lg-split">
          <Reveal>
            <h2 className="lg-h2" style={{ fontSize: 'clamp(28px, 3.2vw, 42px)', margin: 0 }}>
              Five answers. One of them is “nobody ruled on this.”
            </h2>
            <p className="lg-body" style={{ marginTop: 18 }}>
              A boolean cannot say <em>the check never ran</em>. This one can, because a
              missing judgement is a distinct outcome rather than a quiet pass.
            </p>
          </Reveal>

          <Reveal delay={80}>
            <Gate />
          </Reveal>
        </div>

        <Reveal delay={120}>
          <ul className="lg-vocab">
            {STATUSES.map((s) => (
              <li key={s.term}>
                <Status tone={s.tone}>{s.term}</Status>
                <span className="lg-vocab-gloss">{s.gloss}</span>
              </li>
            ))}
          </ul>
        </Reveal>
      </div>
    </section>
  );
}

/* ==========================================================================
   Run it
   ========================================================================== */

const COMMANDS = [
  { c: 'copy .env.example .env', note: 'fill in the keys' },
  { c: 'python backend/scripts/db_migrate.py', note: 'apply the schema' },
  { c: 'set PYTHONPATH=backend && python -m uvicorn app.main:app --port 8000', note: '' },
  { c: 'opencode serve --port 4096 --hostname 127.0.0.1', note: 'judges + extraction' },
  { c: 'cd frontend && npm install && npm run dev', note: 'http://localhost:5173' },
];

function RunIt() {
  return (
    <section className="lg-section" id="run">
      <div className="lg-wrap">
        <div className="lg-split">
          <Reveal>
            <h2 className="lg-h2" style={{ fontSize: 'clamp(28px, 3.2vw, 42px)', margin: 0 }}>
              Check the rail yourself.
            </h2>
            <p className="lg-body" style={{ marginTop: 18 }}>
              Ask a question you already know the answer to, and watch what happens to the
              fields nobody could prove.
            </p>
          </Reveal>

          <Reveal delay={80}>
            <div className="lg-stack">
              {COMMANDS.map((cmd) => (
                <div className="lg-cmd" key={cmd.c}>
                  <code>{cmd.c}</code>
                  {cmd.note ? <span className="lg-tag">{cmd.note}</span> : null}
                  <CopyCmd text={cmd.c} />
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

/* ==========================================================================
   Page
   ========================================================================== */

const NAV = [
  { href: '#record', label: 'The record' },
  { href: '#explainer', label: 'Explainer' },
  { href: '#pipeline', label: 'Pipeline' },
  { href: '#sufficiency', label: 'The gap' },
  { href: '#vocabulary', label: 'Vocabulary' },
  { href: '#run', label: 'Run it' },
];

function Nav() {
  const [pct, setPct] = useState(0);

  useEffect(() => {
    let frame = 0;
    const read = () => {
      frame = 0;
      const max = document.documentElement.scrollHeight - window.innerHeight;
      setPct(max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(read);
    };
    read();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, []);

  return (
    <header className="lg-nav">
      <div className="lg-wrap lg-nav-in">
        <a className="lg-brand" href="#top">
          <img src="/mark-56.png" alt="" width={26} height={26} />
          DataGoblin
        </a>
        <nav className="lg-nav-links" aria-label="Sections">
          {NAV.map((n) => (
            <a key={n.href} className="lg-nav-link" href={n.href}>
              {n.label}
            </a>
          ))}
          <a className="lg-cta lg-cta-primary" href="#run" style={{ marginLeft: 10, height: 32 }}>
            Run it
          </a>
        </nav>
      </div>
      <div className="lg-progress" style={{ transform: `scaleX(${pct})` }} aria-hidden="true" />
    </header>
  );
}

export default function Landing() {
  const root = useReveal();

  return (
    <div className="lg" id="top" ref={root}>
      <AmbientField />
      <Nav />

      <main>
        {/* --- hero: the record is the thesis ---------------------------- */}
        <section className="lg-hero" id="record">
          <div className="lg-wrap lg-hero-grid">
            <div>
              <Reveal>
                <h1 className="lg-h1">Stop shipping data you can’t defend in a review.</h1>
              </Reveal>
              <Reveal delay={80}>
                <p className="lg-lede lg-hero-lede">
                  Every non-null field carries the exact stored text it came from — a quote,
                  character offsets, a page id, a content hash. No stored sentence, no value.
                </p>
              </Reveal>
              <Reveal delay={150}>
                <div className="lg-hero-actions">
                  <a className="lg-cta lg-cta-primary" href="#run">
                    Run it against the record
                  </a>
                  <a className="lg-cta lg-cta-ghost" href="#sufficiency">
                    Where this still fails
                  </a>
                </div>
              </Reveal>
              <Reveal delay={210}>
                <Receipt />
              </Reveal>
            </div>

            <Reveal delay={120}>
              <Dossier />
            </Reveal>
          </div>
        </section>

        <div className="lg-rule" />
        <Explainer />
        <div className="lg-rule" />
        <Pipeline />
        <div className="lg-rule" />
        <Sufficiency />
        <div className="lg-rule" />
        <Vocabulary />
        <div className="lg-rule" />
        <RunIt />
      </main>

      <footer className="lg-foot">
        <div className="lg-wrap lg-foot-in">
          <span className="lg-tag">DataGoblin · evidence-first data collection</span>
          <a href="#top">Back to top</a>
          <a href="#record">The record</a>
          <a href="#run">Run it</a>
          <span className="lg-fine lg-foot-note">
            Figures on this page are a real run, {P.run_id.slice(0, 8)}, re-fetched{' '}
            {P.refetched_on}. Permitted sources only; robots.txt honored; full SSRF check.
          </span>
        </div>
      </footer>
    </div>
  );
}