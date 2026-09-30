import { certaintyOf, CERTAINTY_LABEL, CERTAINTY_HINT, CERTAINTY_CELLS, CERTAINTY_TONE } from '../lib/plain.js';
import { toneVar } from '../lib/format.js';

/**
 * How sure we are, as a three-cell meter and a word.
 *
 * Two decisions worth stating, because they are what make it honest rather than
 * decorative.
 *
 * **The word is always rendered.** The meter's colour is redundant with the
 * number of lit cells, and the word is redundant with both. That is the point:
 * colour is never the only thing carrying the meaning, so it survives a
 * greyscale print, a colour-blind reader and a high-contrast mode. A reader who
 * cannot distinguish the green from the amber still reads "Check this".
 *
 * **A missing confidence reads as "Check this", never as "Sure".** `certaintyOf`
 * returns `check` for anything it cannot parse, so a cell with no confidence
 * fails towards the cautious answer. A component that defaulted to the confident
 * end would quietly upgrade every unmeasured value.
 *
 * Colours go through `toneVar`, which wraps the raw RGB triplet in `rgb()` — the
 * project's token guard requires every colour token to be wrapped, and a literal
 * hex here would fail it.
 *
 * The meter itself is `aria-hidden`; the accessible text is the word beside it.
 */
export default function CertaintyMark({ confidence, certainty, hint }) {
  const level = certainty || certaintyOf(confidence ?? 0);
  const lit = CERTAINTY_CELLS[level] ?? 1;
  const tone = CERTAINTY_TONE[level] ?? 'muted';
  const label = CERTAINTY_LABEL[level] ?? CERTAINTY_LABEL.check;
  const why = hint || CERTAINTY_HINT[level] || '';

  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] leading-none"
      title={why}
    >
      <span className="flex gap-0.5" aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 rounded-[1px]"
            style={i < lit ? { background: toneVar(tone) } : undefined}
            data-off={i >= lit ? '' : undefined}
          />
        ))}
      </span>
      <span className="text-muted">{label}</span>
    </span>
  );
}
