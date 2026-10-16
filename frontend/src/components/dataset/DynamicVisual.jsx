import { Histogram, RankBars, Timeline, Treemap } from '../ui.jsx';
import { shapeFor } from '../../lib/datasetViewModel.js';
import { num, truncate } from '../../lib/format.js';

/**
 * Schema-driven visualisation.
 *
 * DataGoblin does not know whether a dataset holds job listings, restaurant
 * reviews, product prices or papers, and hard-coding a chart per vertical is how
 * a scraper turns into a pile of rigid dashboards. Instead the *profiler* has
 * already read every field and decided each one's `kind` from its values, and
 * this component picks the representation that kind can honestly support.
 *
 *   date       → timeline, empty years left visible
 *   numeric    → histogram with the median drawn
 *   categorical→ treemap beside a ranked list
 *   scalar     → a sentence, because one bar is a sentence with axes
 *   empty      → a sentence, because a zero-height bar is a different problem
 *
 * The refusals live in `shapeFor` (`lib/datasetViewModel.js`) rather than here,
 * so they are testable without a browser and cannot drift between call sites.
 * This component renders what that decision allows and nothing more.
 *
 * Every chart is paired with its own value column. `Treemap` draws no in-tile
 * labels — its SVG is stretched to the container, so viewBox text cannot be
 * sized against a tile — and a number that is only reachable by hovering a
 * shape is not readable data. Nothing here can draw a picture of something the
 * stored values do not support, because the widgets themselves refuse to.
 */

const RANK_LIMIT = 6;

function Sentence({ children }) {
  return <p className="text-[11.5px] leading-snug text-muted">{children}</p>;
}

/** Categorical: shape for the eye, exact counts for the reader. */
function Ranked({ shape, dense = false }) {
  const values = shape.values || [];
  if (!values.length) return null;
  return (
    <div className="space-y-2">
      {values.length > 1 ? (
        <Treemap
          height={dense ? 96 : 132}
          items={values.map((v) => ({
            key: String(v.value),
            label: String(v.value),
            value: v.n,
            n: v.n,
          }))}
        />
      ) : null}
      <RankBars
        items={values.map((v) => ({
          key: String(v.value),
          label: truncate(String(v.value), 34),
          value: v.n,
        }))}
        total={shape.total}
        limit={RANK_LIMIT}
        showShare={false}
      />
      {shape.truncated ? (
        <p className="text-[10.5px] text-muted">More distinct values exist than are listed here.</p>
      ) : null}
    </div>
  );
}

/**
 * Draw whatever this field's shape supports, or say why nothing can be drawn.
 *
 * `dense` is for the overview strip, where several panels share one row and a
 * 130px treemap would push everything below the fold. It changes sizes only —
 * never which representation is chosen.
 */
export default function DynamicVisual({ field, dense = false, className = '' }) {
  const shape = shapeFor(field);
  if (!shape) return null;

  if (shape.kind === 'empty') {
    return (
      <Sentence>
        Declared by the schema, filled on no record. Re-reading these pages may not help — the
        sources may simply not carry it.
      </Sentence>
    );
  }

  if (shape.kind === 'scalar') {
    return (
      <Sentence>
        One distinct value across {num(field.records)} record
        {field.records === 1 ? '' : 's'}, so there is no distribution to draw.
      </Sentence>
    );
  }

  if (shape.kind === 'point') {
    return <Sentence>{shape.why.charAt(0).toUpperCase() + shape.why.slice(1)}.</Sentence>;
  }

  if (shape.kind === 'timeline') {
    return <Timeline byYear={shape.byYear} min={shape.min} max={shape.max} height={dense ? 44 : 56} />;
  }

  if (shape.kind === 'histogram') {
    return (
      <Histogram buckets={shape.buckets} median={shape.median} format={num} height={dense ? 44 : 64} />
    );
  }

  if (shape.kind === 'rank') {
    return <Ranked shape={shape} dense={dense} />;
  }

  return null;
}

/** The field's detected kind, for a pill. `null` when there is nothing to say. */
export function kindTone(field) {
  const kind = field && field.kind;
  if (kind === 'numeric') return { tone: 'judge', label: 'numeric' };
  if (kind === 'categorical') return { tone: 'info', label: 'categorical' };
  if (kind === 'date') return { tone: 'accent', label: 'temporal' };
  if (kind === 'empty') return { tone: 'warn', label: 'never filled' };
  if (kind === 'scalar') return { tone: 'muted', label: 'constant' };
  return null;
}