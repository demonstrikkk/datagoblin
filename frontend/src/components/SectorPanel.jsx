import { Histogram, RankBars, Timeline, Treemap } from './ui.jsx';
import { num } from '../lib/format.js';

/**
 * What this dataset is, and what that means for reading it.
 *
 * The classifier in the backend returns a receipt, not a label: the sector, a
 * confidence, and the fields that argued for it. All three are shown. A panel
 * that said "Markets" with no visible basis would be an assertion, and an
 * assertion here is what the rest of this product exists to avoid.
 *
 * When the classifier returns null the panel says so and explains what it would
 * have needed. That is not an empty state to be filled — it is the honest
 * answer, and the sector-agnostic field view above it still shows everything.
 *
 * The panels below are all existing widgets. `Histogram` and `Timeline` return
 * null below two buckets or rows, and `Treemap` draws no in-tile labels, so
 * every one of them needs a value column beside it — which is why each section
 * pairs a chart with a ranked list. Nothing here can draw a shape the data
 * cannot support, because the widgets refuse to.
 */

const SECTOR_LABEL = {
  stocks: 'Markets',
  healthcare: 'Clinical',
  education: 'Education',
  realestate: 'Property',
  energy: 'Energy',
  hr: 'Workforce',
};

/** Why the classifier thinks so, in a sentence a person can check. */
function receipt(sector) {
  const fields = (sector.matched_fields || [])
    .filter((m) => m.score >= 0.5)
    .map((m) => m.field);
  if (!fields.length) return null;
  const shown = fields.slice(0, 4).join(', ');
  const more = fields.length > 4 ? ` and ${fields.length - 4} more` : '';
  return `read from ${shown}${more}`;
}

export default function SectorPanel({ profile, status, error, onRetry }) {
  /*
   * The panel sits above the dataset's tab bar, so it has to survive a profile
   * request that is still in flight or that failed. Rendering `null` for a
   * pending request would make the panel flicker in and out on every open; a
   * silent retry button would hide a real failure. So: say which of the two it
   * is.
   */
  if (error) {
    return (
      <section className="surface p-3.5">
        <h2 className="eyebrow">Domain</h2>
        <p className="mt-1.5 text-[12.5px] text-ink-2">
          The field profile could not be read, so no domain was assigned.
        </p>
        {onRetry ? (
          <button type="button" onClick={onRetry} className="btn-outline btn-xs mt-2">
            Retry profile
          </button>
        ) : null}
      </section>
    );
  }

  if (!profile && status === 'loading') {
    return (
      <section className="surface p-3.5">
        <h2 className="eyebrow">Domain</h2>
        <p className="mt-1.5 text-[12.5px] text-muted">Reading every field to place this dataset…</p>
      </section>
    );
  }

  const sector = profile?.sector || null;
  if (!sector) {
    return null;
  }

  if (!sector.sector) {
    return (
      <section className="surface p-3.5">
        <h2 className="eyebrow">Domain</h2>
        <p className="mt-1.5 text-[12.5px] text-ink-2">
          No domain was assigned to this dataset.
        </p>
        <p className="mt-1 text-[11.5px] leading-relaxed text-muted">
          {sector.method === 'ambiguous'
            ? 'The fields point at more than one kind of dataset, so none was chosen. Everything is still shown below, arranged by field rather than by domain.'
            : sector.method === 'below-floor'
            ? `The closest reading scored ${sector.confidence}, below the ${sector.floor} needed to be sure. Everything is shown below, arranged by field rather than by domain.`
            : 'The schema does not say what kind of dataset this is. Everything is shown below, arranged by field rather than by domain.'}
          {sector.considered?.length ? ` Considered: ${sector.considered.slice(0, 3).map((c) => `${c.sector} (${c.score})`).join(', ')}.` : ''}
        </p>
      </section>
    );
  }

  const byName = new Map((profile.fields || []).map((f) => [f.field, f]));
  const fields = sector.matched_fields.map((m) => byName.get(m.field)).filter(Boolean);
  const why = receipt(sector);

  return (
    <section className="space-y-3">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h2 className="eyebrow">Domain</h2>
          <p className="mt-0.5 h-display text-[20px] leading-tight text-ink">
            {SECTOR_LABEL[sector.sector] || sector.sector}
          </p>
        </div>
        <p className="text-[10.5px] text-muted">
          {why ? `${why} · ` : ''}confidence {sector.confidence}
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {fields.slice(0, 6).map((f) => (
          <FieldPanel key={f.field} field={f} />
        ))}
      </div>

      {fields.length ? null : (
        <p className="text-[11.5px] text-muted">
          No profile data for the fields this reading rests on.
        </p>
      )}
    </section>
  );
}

/**
 * One domain-relevant field, drawn only if its own data supports a shape.
 *
 * Numeric fields get a histogram, dates a timeline, categories a treemap beside
 * a ranked list. A `scalar` or `empty` field gets a sentence and no chart —
 * which is the profiler's own rule, restated here because this is the second
 * place it is applied.
 */
function FieldPanel({ field }) {
  const shape = field.shape || {};
  const trust = field.trust || {};

  return (
    <div className="surface p-3">
      <div className="flex items-baseline justify-between gap-2">
        <p className="eyebrow truncate">{field.field}</p>
        <p className="shrink-0 text-[10.5px] tabular-nums text-muted">
          {num(trust.present || 0)}/{num(field.records || 0)}
        </p>
      </div>

      {field.kind === 'numeric' && shape.buckets?.length ? (
        <div className="mt-1.5">
          <Histogram buckets={shape.buckets} median={shape.median} format={num} height={56} />
        </div>
      ) : null}

      {field.kind === 'date' && shape.by_year?.length ? (
        <div className="mt-1.5">
          <Timeline byYear={shape.by_year} min={shape.min} max={shape.max} height={52} />
        </div>
      ) : null}

      {field.kind === 'categorical' && shape.values?.length ? (
        <div className="mt-1.5 space-y-2">
          {shape.values.length > 1 ? (
            <Treemap
              items={shape.values.map((v) => ({
                key: String(v.value),
                label: String(v.value),
                value: v.n,
                n: v.n,
              }))}
              height={104}
            />
          ) : null}
          <RankBars
            items={shape.values.map((v) => ({
              key: String(v.value),
              label: String(v.value),
              value: v.n,
            }))}
            total={shape.values.reduce((a, v) => a + v.n, 0)}
            limit={6}
            showShare={false}
          />
        </div>
      ) : null}

      {field.kind === 'scalar' || field.kind === 'empty' ? (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">
          {field.kind === 'empty'
            ? 'No record carries a value for this field.'
            : `One distinct value across ${num(field.records || 0)} records, so there is no distribution to draw.`}
        </p>
      ) : null}
    </div>
  );
}
