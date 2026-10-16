import DynamicVisual from './DynamicVisual.jsx';
import RecordsTable from './RecordsTable.jsx';
import { Empty, Notice } from '../ui.jsx';
import { num } from '../../lib/format.js';

/**
 * What this dataset turned out to hold.
 *
 * This panel answers the question someone opens a dataset to ask, which none of
 * the other sections do: not how many rows there are, but what is in them. The
 * old first screen answered it with record count, proven-field count and schema
 * size, which are properties of the container rather than of the data.
 *
 * It is built from three parts, in priority order:
 *
 *   1. what was found — lines derived from counts the backend measured
 *   2. what shape it has — the fields whose shape is real enough to draw
 *   3. the ledger itself — a reader should not have to leave to see a row
 *
 * WHY THERE IS NO AI INSIGHTS BOX
 *
 * The obvious design here is a panel labelled "AI insights" that summarises the
 * dataset in prose. It is refused, for the same reason the export format list in
 * `docs/18-DESIGN-SYSTEM.md` refuses fake certainty: this page can only report
 * what was measured. Every line under "What was found" is arithmetic over stored
 * numbers and carries the sentence that proves it, which is why the lines are
 * phrased as findings with denominators ("47 of 120 records") rather than as
 * conclusions ("roles are concentrated in Delhi").
 *
 * The difference is visible in the one thing a careful buyer checks first: this
 * panel cannot say "up 8% on last month", because this page has no access to a
 * previous run's numbers, and a panel that could say it would be lying.
 */

/* -------------------------------------------------------------- what it found */

const TONE_CLASS = {
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  info: 'text-info',
  muted: 'text-ink-2',
};

function Findings({ findings, onOpen, className = '' }) {
  if (!findings.length) return null;
  return (
    <section className={className}>
      <h2 className="eyebrow">What the run found</h2>
      <ol className="mt-2 space-y-2.5">
        {findings.map((f, i) => (
          <li key={f.id} className="flex gap-2.5">
            <span className="mt-px shrink-0 font-mono text-[10.5px] tnum text-rule-2">
              {String(i + 1).padStart(2, '0')}
            </span>
            <div className="min-w-0">
              {f.lead ? (
                <p className="h-display text-[15px] leading-snug text-ink">{f.lead}</p>
              ) : null}
              <p className={`text-[12.5px] leading-relaxed ${TONE_CLASS[f.tone] || TONE_CLASS.muted}`}>
                {f.text}
              </p>
              {f.detail ? (
                <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{f.detail}</p>
              ) : null}
              {f.target && onOpen ? (
                <button
                  type="button"
                  onClick={() => onOpen(f.target)}
                  className="link mt-1 text-[10.5px]"
                >
                  go to {f.target === 'coverage' ? 'gaps' : f.target} →
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/* ---------------------------------------------------------------- data shape */

function ShapePanel({ shape, record }) {
  const { field, shape: s } = record;
  const trust = field.trust || {};

  return (
    <article className="surface flex min-w-0 flex-col gap-2 p-3">
      <header className="flex items-baseline justify-between gap-2">
        <h4 className="truncate font-mono text-[11.5px] text-ink" title={field.field}>
          {field.field}
        </h4>
        <span className="shrink-0 font-mono text-[10px] tnum text-muted">
          {num(trust.present || 0)}/{num(field.records || 0)}
        </span>
      </header>

      <DynamicVisual field={field} dense />

      {/* What kind of thing this is, so the shape above is not a mystery. The
          profiler decided it from the values, not from the field's name. */}
      <footer className="mt-auto flex items-center justify-between gap-2 border-t border-rule pt-1.5 text-[10px] text-muted">
        <span>{field.kind}</span>
        {trust.conflicting ? (
          <span className="text-danger">{num(trust.conflicting)} disputed</span>
        ) : null}
      </footer>
    </article>
  );
}

/* -------------------------------------------------------------------- domain */

/**
 * The domain reading, with its receipt.
 *
 * Kept as one compact line rather than the three-column chart wall it used to
 * be. The classifier returns a sector, a confidence and the fields that argued
 * for it; showing all three is the point, and showing them at 3x the size they
 * need stops being a receipt and starts being a claim.
 */
function DomainLine({ sector, onOpen }) {
  if (!sector) return null;

  if (!sector.sector) {
    return (
      <Notice tone="muted">
        <span className="font-medium text-ink-2">No domain was assigned.</span>{' '}
        {sector.method === 'ambiguous'
          ? 'The fields point at more than one kind of dataset, so none was chosen.'
          : sector.method === 'below-floor'
          ? `The closest reading scored ${sector.confidence}, below the ${sector.floor} needed to be sure.`
          : 'The schema does not say what kind of dataset this is.'}{' '}
        Everything below is arranged by field rather than by domain.
      </Notice>
    );
  }

  const argued = (sector.matched_fields || []).filter((m) => m.score >= 0.5).slice(0, 4);
  return (
    <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
      <span className="eyebrow">Domain</span>
      <span className="h-display text-[17px] leading-none text-ink">
        {sector.sector.charAt(0).toUpperCase() + sector.sector.slice(1)}
      </span>
      <span className="text-[10.5px] text-muted">
        read from {argued.map((m) => m.field).join(', ') || 'the schema'} · confidence{' '}
        {sector.confidence}
      </span>
      {onOpen ? (
        <button type="button" onClick={() => onOpen('fields')} className="link text-[10.5px]">
          see the fields →
        </button>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ overview */

export default function DatasetOverview({ model, onOpen, onPick, onCell, datasetId, schema }) {
  const { findings = [], shapes = [], health, identity, sector } = model;

  const weakest = health
    ? [health.coverage, health.evidence, health.freshness, health.consistency]
        .filter((h) => h.counted)
        .sort((a, b) => a.value - b.value)[0]
    : null;

  return (
    <div className="space-y-6">
      {/*
        The ledger comes first on the canvas, not last.

        A reader who opened this dataset came to look at data, and the previous
        layout spent its entire first screen on metadata before the first row.
        The findings and the shapes sit above it because they are short, and
        because a reader who wants the rows can scroll past three paragraphs.
      */}
      <section aria-label="Record explorer">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="eyebrow">Records</h2>
          <div className="flex flex-wrap items-center gap-2">
            {weakest ? (
              <span className="text-[10.5px] text-muted">
                weakest signal: {weakest.label.toLowerCase()} at {weakest.value}%
              </span>
            ) : null}
            <button type="button" onClick={() => onOpen('records')} className="link text-[10.5px]">
              open the full ledger →
            </button>
          </div>
        </div>
        <RecordsTable
          datasetId={datasetId}
          schema={schema}
          onPick={onPick}
          onCell={onCell}
          pageSize={25}
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Findings findings={findings} onOpen={onOpen} />

        <section aria-label="Data shape" className="min-w-0">
          <div className="flex items-baseline justify-between gap-2">
            <h2 className="eyebrow">Shape of the data</h2>
            <button type="button" onClick={() => onOpen('fields')} className="link text-[10.5px]">
              every field →
            </button>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            {identity.fieldCount
              ? `Which of these ${num(identity.fieldCount)} fields can honestly be drawn, and how.`
              : 'The profile has not been read yet.'}{' '}
            A field with one value, or none, gets a sentence instead of a chart.
          </p>

          {shapes.length ? (
            <div className="mt-2.5 grid gap-3 sm:grid-cols-2">
              {shapes.map((rec) => (
                <ShapePanel key={rec.field.field} record={rec} />
              ))}
            </div>
          ) : (
            <div className="mt-2.5">
              <Empty
                title="No field has a shape to draw"
                hint="Either every field holds a single value, or the profile is still being read. Both are answers."
              />
            </div>
          )}
        </section>
      </div>

      <DomainLine sector={sector} onOpen={onOpen} />
    </div>
  );
}