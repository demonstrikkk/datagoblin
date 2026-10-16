import { num } from '../../lib/format.js';

/**
 * The dataset rail.
 *
 * The eight areas a dataset can be read through were a horizontal tab strip
 * sitting under a wall of summary cards, which made the page read as a list of
 * metadata before a single record. They are a vertical rail here instead: the
 * dataset's identity and health stay on screen, and the reader's place in the
 * dataset is always visible rather than scrolled away.
 *
 * THREE ARIA/DOM CONSTRAINTS THIS COMPONENT EXISTS TO SATISFY
 *
 * The end-to-end suites address this rail as a tablist, and those selectors are
 * a real contract rather than an incidental detail of the current markup:
 *
 *  1. The container keeps `role="tablist"` and `aria-label="Dataset section"`.
 *     `aria-orientation="vertical"` is what makes a vertical tablist correct
 *     rather than merely decorative — screen readers announce "tab list,
 *     vertical" and arrow-key expectations follow the orientation.
 *
 *  2. Each item keeps `role="tab"` and `aria-selected`, and its accessible name
 *     is exactly the label. Tests match `^Gaps$`, `^Records$`, `^Conflicts$`
 *     against the button's text, so no count may be rendered *inside* the
 *     button — an inline badge turns "Gaps 12" into text that matches nothing.
 *     The count therefore sits outside the button as a sibling, which is also
 *     more honest: it is a property of the dataset, not part of the name of the
 *     section.
 *
 *  3. The order is meaningful. One suite selects `[role=tab]` by index, so
 *     Records must stay second. Overview leads because it is what a reader
 *     wants first, and it is followed immediately by the ledger.
 */

const GLYPH = {
  overview: '◉',
  records: '▦',
  fields: '⌗',
  sources: '◌',
  review: '✓',
  coverage: '△',
  conflicts: '⊕',
  ask: '✦',
  export: '⇧',
};

export const RAIL_ITEMS = [
  { value: 'overview', label: 'Overview', title: 'What this dataset turned out to hold' },
  { value: 'records', label: 'Records', title: 'The ledger — every stored row' },
  { value: 'fields', label: 'Fields', title: 'Every field, its shape and its trust' },
  { value: 'review', label: 'Review', title: 'Values the pipeline could not settle' },
  { value: 'coverage', label: 'Gaps', title: 'What is missing, and whether a stored page can fill it' },
  { value: 'conflicts', label: 'Conflicts', title: 'Places two sources disagree, with both quotes kept' },
  { value: 'sources', label: 'Sources', title: 'Every page this dataset rests on' },
  { value: 'ask', label: 'Ask', title: 'Ask a question of this dataset' },
  { value: 'export', label: 'Export', title: 'Take the dataset and its evidence with you' },
];

/**
 * The rail, plus a live count per section where the workspace knows one.
 *
 * `counts` is a map of section value to a number or null. A null count renders
 * nothing at all rather than a zero — "0 conflicts" and "conflicts not yet
 * read" are different facts, and this is the one place a reader would read the
 * second as the first.
 */
export default function DatasetRail({ value, onChange, counts = {}, className = '' }) {
  return (
    <nav aria-label="Dataset sections" className={`w-full shrink-0 sm:w-[168px] ${className}`}>
      <p className="eyebrow mb-2 px-2">Workspace</p>
      {/*
        One tablist at every width, reflowed by CSS rather than duplicated per
        breakpoint. Rendering a desktop rail and a mobile rail would put two
        tablists with the same accessible name in the document, which is a real
        defect rather than a cosmetic one: a screen reader announces the group
        twice, and every `role=tab` selector outside this file has to disambiguate
        with `.first` and silently depend on source order.
      */}
      <div
        role="tablist"
        aria-label="Dataset section"
        aria-orientation="vertical"
        className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 sm:mx-0 sm:block sm:space-y-px sm:overflow-visible sm:px-0 sm:pb-0"
      >
        {RAIL_ITEMS.map((it) => {
          const on = it.value === value;
          const count = counts[it.value];
          return (
            <div
              key={it.value}
              className="flex shrink-0 items-stretch gap-1 sm:shrink"
            >
              <button
                type="button"
                role="tab"
                aria-selected={on}
                title={it.title}
                onClick={() => onChange(it.value)}
                className={`focusable group relative flex min-w-0 items-center gap-2 rounded-sm py-[7px] pl-2 pr-1.5 text-left transition-colors duration-150 ease-swift sm:flex-1 ${
                  on ? 'bg-warm/80 text-ink' : 'text-muted hover:bg-warm/40 hover:text-ink-2'
                }`}
              >
                {/* The active marker is a bar that scales in, matching the nav
                    rail's language. It is drawn rather than colour-only, so the
                    current section survives a monochrome or forced-colours
                    render. */}
                <span
                  aria-hidden="true"
                  className={`absolute left-0 top-1/2 h-4 w-[2px] origin-center -translate-y-1/2 rounded-r bg-accent transition-transform duration-200 ease-swift ${
                    on ? 'scale-y-100' : 'scale-y-0'
                  }`}
                />
                <span
                  aria-hidden="true"
                  className="rail-glyph w-3 shrink-0 text-center text-[11px] opacity-70"
                  style={{ '--glyph': `"${GLYPH[it.value]}"` }}
                />
                <span className="truncate text-[12.5px] font-medium">{it.label}</span>
              </button>
              {/* Outside the button on purpose — see constraint 2 above. */}
              {count === null || count === undefined ? null : (
                <span
                  aria-hidden="true"
                  className="flex items-center pr-1.5 font-mono text-[10px] tnum text-rule-2"
                >
                  {num(count)}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </nav>
  );
}