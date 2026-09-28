# 18 — DESIGN SYSTEM

Rewritten after the 2026-09-27 rebuild. The palette and direction below are
carried forward from the original spec; the token mechanics, motion scale and
component rules are new and describe the code that exists.

**Direction:** a quiet, editorial, investigative data workspace. The dataset is
the hero and the evidence is one click away. Not a generic AI dashboard — no
purple gradients, no glassmorphism, no neon, no floating orb, no chat
everywhere, no Inter.

## Colour

Tokens are **bare RGB channel triplets**, not hex:

```css
--paper: 246 243 238;
--ink:   28  25  23;
```

and Tailwind declares them as
`rgb(var(--paper) / <alpha-value>)`.

This is not stylistic. Tailwind can only resolve an opacity modifier
(`bg-paper-2/90`) against a colour declared in the `rgb(var(--x) /
<alpha-value>)` form. With a plain `var(--x)` hex token the modifier fails to
compile and the build errors out. Raw CSS writes the colour as `rgb(var(--x))`.

| Token | Value | Use |
|---|---|---|
| `paper` | `#f6f3ee` | page base |
| `paper-2` | `#fbf9f6` | surfaces |
| `paper-3` | `#ffffff` | raised: inputs, menus |
| `ink` | `#1c1917` | primary text |
| `ink-2` | `#44403c` | secondary text |
| `muted` | `#79716b` | labels, metadata |
| `warm` | `#ede7dd` | insets, tracks, skeletons |
| `rule` / `rule-2` | `#e3ddd3` / `#d3cabb` | borders |
| `accent` | `#9a6b3f` | active state, primary action |
| `ok` `warn` `danger` `info` `judge` | — | verification semantics |

Tinted status fills (`ok-soft`, `warn-soft`, …) are real tokens rather than
arbitrary `bg-[var(--x-soft)]` values, so they can be `@apply`-ed and stay in
sync with their base colour.

### The five verification tones are not interchangeable

`ok` verified · `muted` unverified · `danger` conflicting · `info` no judgment ·
`judge` rate-limited. `judgment_unavailable` and `rate_limited` are deliberately
distinct from `verified`: one means the quote was found but no judge was
reachable, the other means the judge said "wait". Collapsing either into
"verified" is how a summary starts lying.

## Typography

- Display: **Instrument Serif** — page and section headings only.
- UI: **DM Sans**.
- Numerals: **DM Mono** with `font-variant-numeric: tabular-nums` for every
  count, so columns align and numbers do not jitter as they update.
- `.eyebrow` — 10px, 600, `0.14em` tracking, uppercase, muted. All metadata
  labels. Note this is CSS `text-transform`, which means any test reading
  rendered text gets it back uppercased.

## Motion

Easing: `--e-swift: cubic-bezier(.22,.61,.36,1)` for UI,
`--e-spring: cubic-bezier(.34,1.42,.5,1)` for presses and toggles,
`--e-exit: cubic-bezier(.4,0,1,1)` for exits.

Named keyframes: `fade-up`, `fade-in`, `scale-in`, `slide-left`, `rise-in`,
`shimmer`, `pulse-ring`, `dash-flow`.

### The rule that governs all of it

**Motion means work is happening.** The only continuously animating element
during a run is the active stage on the rail. A static screen is never
decorated into looking busy, and no animation is purely ornamental on a
control the user can act on.

`prefers-reduced-motion: reduce` collapses all durations to ~0, stops the
skeleton shimmer, and disables the live dot's pulse. The background field
renders exactly one static frame and stops.

## Layout

- Rail 60px, inspector 400px, page measure capped at 1180px, gutter 20px.
- A single focused task (`Collect`) is centred at 720px with `pt-[7vh]` rather
  than stretched across the full measure — a 30-character line across 1180px
  is not readable, and padding a mostly-empty page is worse than giving the
  space to something useful (recent runs).
- Data surfaces are **near-opaque** (`bg-paper-2/90`). The WebGL field is
  decorative and must never affect text contrast, so nothing readable depends
  on it. `backdrop-blur` is confined to the two small chrome strips that need
  it, because blurring over an animating canvas on every frame is the single
  most expensive thing this app could do.

## Components

- **Buttons** — `.btn` + a variant: primary (ink), accent (bronze), outline,
  ghost, danger. All have `active:scale-[.975]`, so presses feel physical.
- **Inputs** — `.input` with a 4px accent focus ring. The ring is the only
  place the accent appears at that size.
- **Rows** — `.row` uses a `::after` overlay for hover tint and a `::before`
  left bar for the active row, so neither shifts layout.
- **Pills** — status, always glyph + label + `title`, never colour alone.
- **Stat** — label, tabular number, and a sub-line that always qualifies the
  number ("20% of 3,474 fields", not a bare figure).
- **StackedBar** — the primary "how good is this data" read. One glance, no
  legend hunting, and every segment is a click target so it doubles as a
  filter. Segments dim when a sibling filter is active.
- **Skeleton** — shimmer on the `--warm` track. Used only where the shape of
  the arriving content is genuinely known.
- **Empty** — states what is missing and what to do, never a bare "No data".
- **ErrorNote** — a specific message plus a retry, never "Something went wrong".

## Data visualisation

There is no chart library, so the rules had to be written down:

- **Proportion, not quantity, for quality.** Verification is always a
  proportional bar with counts in a legend. A count alone invites the reader
  to treat 217 as good without seeing the 200 beside it.
- **Absence of counters is not absence of verification.** Several runs wrote no
  aggregate counters while still storing per-field status. Those say "not
  counted" — never "unproven", which is a confident negative the data does not
  support.
- **Record-level and field-level are different claims.**
  `records_fully_verified` means every field on that record was verified. It is
  never presented as "verified fields".
- **Large numbers get separators and a gloss.** Raw extraction emits
  `17000000000`; that renders as `17,000,000,000 (17,000M)` with the exact
  value in the tooltip, because rounding a figure a reader may rely on is its
  own kind of lie.

## The proof inspector

The visual identity of the product is a single expandable evidence chain:
**value → verification status → verbatim quote → source page → character
offsets → stored page id → content hash → retrieval time.**

It is behind one click per field, because showing 15 quotes for every row would
make the table unreadable and hide the one row a reviewer actually cares about.
A field with no stored page says **"no stored page"** in warning colour. It
never implies verification it cannot support.
