import { toneVar } from '../../lib/format.js';

/**
 * Dataset health — four measurements, not four KPI cards.
 *
 * The previous header spent its first screen on record count, proven fields,
 * schema size and an evidence-quality bar. All four of those facts are real and
 * all four were already on screen; the problem was that they were framed as a
 * scoreboard when they are really a diagnosis. What a reader opening a dataset
 * needs first is not "how big is it" but "where is it weak", so the same
 * information is re-expressed as four signals that each name a place in the
 * workspace worth going to.
 *
 * THE RULE THIS COMPONENT ENFORCES
 *
 * A measurement that was not taken says so. It does not render 0%.
 *
 * Several runs in this database wrote no aggregate counters while still storing
 * per-field status per cell. Four large `0%` figures would be a confident
 * negative that the data does not support, and they would sit directly above a
 * record table whose every value carries a real verification status — the most
 * damaging kind of wrong on this page. So `counted: false` renders "not
 * counted" in muted ink, with no bar.
 *
 * Every counted figure ships with its own denominator as a `title`, because a
 * percentage with no base is a score and this page does not issue scores.
 */

const ORDER = ['coverage', 'evidence', 'freshness', 'consistency'];

function Signal({ signal, onOpen }) {
  const { key, label, value, counted, tone, detail, target } = signal;

  return (
    <button
      type="button"
      onClick={() => onOpen?.(target)}
      disabled={!onOpen}
      title={detail}
      className="focusable group flex min-w-0 flex-col items-start gap-1 rounded-sm px-1.5 py-1 text-left transition-colors duration-150 ease-swift hover:bg-warm/50 disabled:cursor-default disabled:hover:bg-transparent"
    >
      <span className="eyebrow">{label}</span>

      <span className="flex items-baseline gap-1">
        {counted ? (
          <>
            <span
              className="font-mono text-[19px] leading-none tnum"
              style={{ color: toneVar(tone) }}
            >
              {value}%
            </span>
            <span className="text-[9.5px] text-rule-2 opacity-0 transition-opacity duration-150 group-hover:opacity-100">
              {target} →
            </span>
          </>
        ) : (
          <span className="text-[12.5px] leading-none text-muted">not counted</span>
        )}
      </span>

      {/* The bar is the measured proportion, drawn against the full track so an
          absent measurement and a zero measurement never look alike. */}
      <span className="h-[3px] w-full overflow-hidden rounded-full bg-warm" aria-hidden="true">
        {counted ? (
          <span
            className="block h-full rounded-full transition-[width] duration-500 ease-swift"
            style={{ width: `${Math.max(1.5, value)}%`, background: toneVar(tone) }}
          />
        ) : null}
      </span>
    </button>
  );
}

export default function DatasetHealth({ health, onOpen, className = '' }) {
  if (!health) return null;

  const signals = ORDER.map((k) => health[k]).filter(Boolean);
  const uncounted = signals.filter((s) => !s.counted).length;

  return (
    <section aria-label="Dataset health" className={className}>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
        {signals.map((s) => (
          <Signal key={s.key} signal={s} onOpen={onOpen} />
        ))}
      </div>

      {uncounted ? (
        <p className="mt-2 text-[10.5px] leading-snug text-muted">
          {uncounted} of these {uncounted === 1 ? 'signal was' : 'signals were'} not measured for
          this run, rather than measured as zero. The per-field status on any record is still real —
          open the ledger.
        </p>
      ) : null}
    </section>
  );
}