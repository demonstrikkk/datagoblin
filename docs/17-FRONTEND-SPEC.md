# 17 — FRONTEND SPEC

Rewritten after the 2026-09-27 rebuild. The previous version of this document
described a route set (`/new`, `/history`, `/sources`) and a component set
(`PromptComposer`, `ProofDrawer`, `RunTimeline`) that were never fully
implemented. **This document now describes the code that exists.** Where a
decision is a deliberate reversal of the old spec, it says so.

## Stack

React 18, Vite 5, React Router 6, Tailwind 3. **No other dependencies** — no
chart library, no animation library, no state manager, no component kit, no
WebGL library. Every visualisation, animation and ambient effect in this app is
hand-built against React and the platform. That is a deliberate constraint: the
product needs one bar chart, one stage rail and one background field, and each
would otherwise drag in a dependency larger than the whole app.

Dev proxy: `vite.config.js` forwards `/api` to `http://localhost:8000`. The
same proxy applies to `vite preview`, so the production build is testable
locally against a real backend.

## Routes

| Route | View | Purpose |
|---|---|---|
| `/` | `Collect` | Ask → plan → run. The primary flow. |
| `/runs` | `Runs` | Every execution, filterable. |
| `/runs/:id` | `RunPage` | One run, live or finished. |
| `/library` | `Library` | Datasets. |
| `/library/:id` | `DatasetPage` | Records, sources, export. |
| `/intel` | `Intel` | Multi-model fan-out and agreement. |
| `*` | `NotFound` | — |

Each route is a separate lazy chunk. `App.jsx` wraps the router in an error
boundary and a `Suspense` fallback so one broken view cannot take down the
shell.

**Reversal of old spec:** the old document specified `/new` and `/history` as
separate top-level destinations. `Collect` at `/` *is* the command centre, and
history is `/runs`. Six unreachable routes previously existed in the tree
(`/new`, `/datasets/:id`, `/runs/:id` were never linked); every route is now
reachable from the nav rail.

## Layout

```
┌──┬──────────────────────────────────────┬──────────────┐
│  │ CommandBar  title · run chip · ⌘K · health │              │
│R ├──────────────────────────────────────┤  Inspector   │
│a │ main (scroll)                         │  (contextual │
│i │  └ Outlet                             │   right rail)│
│l │                                      │              │
└──┴──────────────────────────────────────┴──────────────┘
   AmbientField (WebGL canvas, fixed, behind everything)
```

- **NavRail** (60px): four destinations plus settings. Active item marked by a
  bronze bar that scales in. Tooltips on hover/focus.
- **CommandBar**: page title, the active run chip (click to jump back into a
  live run), the ⌘K palette trigger, and a health pulse.
- **Inspector**: one contextual slot, right side, 400px. Records, sources,
  events, run details and system diagnostics all open *here* rather than
  pushing new pages.

### Why a single inspector

The alternative was a modal per record. A right rail that never unmounts means
the user's eye learns one place to look for "the detail behind this thing", and
it keeps the main canvas answering only "what is happening". This is the
mechanism that lets the app be dense without being overwhelming.

## Components

| File | Role |
|---|---|
| `AmbientField.jsx` | WebGL background. Raw GL, one draw call. |
| `NavRail.jsx` | Primary navigation, inline SVG icons. |
| `CommandBar.jsx` | Header, run chip, health pulse. |
| `CommandPalette.jsx` | ⌘K: destinations, runs, datasets. |
| `SettingsDialog.jsx` | API key, density, shortcuts, connection. |
| `Shell.jsx` | Layout, routing frame, global hotkeys. |
| `Inspector.jsx` | Contextual detail rail. |
| `RunView.jsx` | Live/finished run: rail, stats, stream, events. |
| `PipelineRail.jsx` | Stage rail + `StageList` + `Counter`. |
| `EventFeed.jsx` | Filterable SSE log. |
| `ui.jsx` | Primitives (Pill, Stat, StackedBar, Field, …). |

**Reversal of old spec:** `ProofDrawer`, `PromptComposer`, `RunTimeline`,
`SourceList`, `DatasetTable`, `ColumnHeader`, `FilterBar`, `DatasetToolbar`,
`HistoryList` and `ExportMenu` were specified individually. They are
consolidated into the components above plus the `ui.jsx` primitive set. Nine
separately-named components for one product surface was over-modularised.

## State

No state library. Three mechanisms cover everything:

1. **Server state** — `hooks/useResource.js`. One `fetcher({signal})`, explicit
   `idle | loading | ready | error`, keeps previous data visible during a
   refetch (`isStale`), aborts on unmount and on dependency change, supports
   polling that pauses when the tab is hidden.
2. **Shared client state** — two React contexts. `inspector.jsx` holds the
   single inspector slot; `run-context.jsx` tracks which run the operator is
   looking at so the header chip and run page never disagree.
3. **Local UI state** — plain `useState`, plus `useLocalStore` for values that
   must survive a reload (last prompt, last run, API key, density).

## Data fetching

`lib/api.js` is the only place that unwraps the `{data, error, meta}` envelope.
Components see `data` or a thrown `ApiError` with a message worth showing.

- Request timeouts, composed with any caller-supplied `AbortSignal`.
- `X-API-Key` sent when set in localStorage.
- Validation errors are flattened from `error.details[]` into readable text
  rather than shown as a raw object.
- **Export is deliberately asymmetric:** `csv` and `md` return a raw file
  attachment that bypasses the envelope entirely, while `json` is a normal
  enveloped response. Requesting a Blob for all three would hand the caller
  `{"data":…}` bytes for JSON.

## The SSE hook

`hooks/useRunStream.js` exists because the naive version was wrong in ways
that were invisible until a run actually finished:

- `run.partial` is **applied** to state. It was previously received and dropped.
- A terminal state **closes the socket**. A `PARTIAL` run is persisted as
  `FAILED + partial=true` and the server-side stream never closes for it, so
  waiting on `run.completed` alone left the UI spinning forever.
- Connection status (`connecting | open | reconnecting | closed`) is exposed
  rather than pretending to be live.
- Events are deduped by a per-type fingerprint, because `EventSource` replays
  recent events on every reconnect.
- Bounded backoff (1s→15s) with the socket torn down on unmount or id change.
- The server sets `event:` to the event type, so `onmessage` alone never fires.
  A named listener is registered for all 13 declared types.

## Keyboard

`⌘K` palette · `1`–`4` navigate · `⌘B` mobile nav · `Esc` close · `↑↓↵` in the
palette.

Bare keys are suppressed while the user is typing — pressing `3` inside a
prompt must not navigate away. Modifier combos are **not** suppressed, so
⌘K works with the caret in a textarea, which is exactly where the user is when
they reach for it.

## Accessibility

- Focus is `:focus-visible` only; no ring on click.
- The event log is `role="log"` with `aria-live="polite"`.
- Verification status is never colour-only: every status carries a glyph and a
  `title` explaining what it means.
- `prefers-reduced-motion` disables decorative animation and stops the
  background field after one static frame.
- Mobile: bottom tab bar, no horizontal overflow at 390px (verified).

## Verification

The build passing proves the modules parse, not that the app works. These are
driven against a real backend with Playwright:

- `smoke.py` — all four routes mount, zero console errors, boot placeholder
  removed, WebGL canvas present, ⌘K opens from a focused textarea, digit
  shortcut navigates, record row expands to a quote + stored page id, sources
  tab renders, no mobile overflow.
- `verify.py` — per-field evidence expansion, sources summary, export panel,
  run table totals.
- `partial.py` — a real partial run: status pill, quality breakdown, the rail
  contains no node labelled "Complete", contradiction check.
- `visual.py` — `readPixels` over the WebGL canvas to prove the field is
  actually drawing (not a flat canvas) and that consecutive frames differ.

Run against `vite preview`, not the dev server: React StrictMode's double
effect produces `net::ERR_ABORTED` on every aborted fetch, which is dev noise
that hides real failures.
