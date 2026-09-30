/**
 * What the reader is told, in their words.
 *
 * A status like `judgment_unavailable` or a gap like `schema_gap` is a precise
 * name for something happening inside the system. It is not a sentence a person
 * reading a dataset needs, and it is not what they are actually asking. They are
 * asking *can I rely on this*. So every internal name crosses into plain words
 * here, in one place, and nothing downstream invents its own phrasing.
 *
 * Two rules, and both of them are about not lying:
 *
 * **An unmapped name is skipped, never printed.** `friendlyLine` returns `null`
 * for an event it has no plain reading for, and the caller drops it. The raw log
 * stays available behind "Show details". The alternative — falling back to the
 * internal name — puts `source.reused` on screen the moment someone adds an
 * event type, which is how internal vocabulary leaks into a product.
 *
 * **A percentage is not a certainty.** `proven_pct: 0.0` describes a score; it
 * does not say whether the dataset is complete, merely unverified, or was never
 * collected at all — three different situations that need three different
 * actions. "Check this" describes the state instead of scoring it, and it is the
 * same three words whatever the number underneath was.
 */

/** How sure we are of a value, in three plain steps rather than a number. */
export function certaintyOf(confidence) {
  const c = Number(confidence);
  if (!Number.isFinite(c)) return 'check';
  if (c >= 0.9) return 'sure';
  if (c >= 0.75) return 'likely';
  return 'check';
}

export const CERTAINTY_LABEL = {
  sure: 'Sure',
  likely: 'Likely',
  check: 'Check this',
};

/** The full sentence behind the short word, for a tooltip. */
export const CERTAINTY_HINT = {
  sure: 'Read straight from the source’s own data',
  likely: 'Read from the page and checked against it',
  check: 'Found, but worth a look before you rely on it',
};

/** How many of the three meter cells are lit, per certainty. */
export const CERTAINTY_CELLS = { sure: 3, likely: 2, check: 1 };

/** Which colour a lit meter cell takes. Colour never carries the meaning alone. */
export const CERTAINTY_TONE = { sure: 'ok', likely: 'muted', check: 'warn' };

/**
 * A cell's verification status, in words.
 *
 * `rate_limited` and `judgment_unavailable` are both "nobody got to check this",
 * and they read differently on purpose: one means come back later, the other
 * means the run ran out of judging budget. Collapsing them into a single "unverified"
 * would hide a condition that is worth knowing about — 992 cells on this workspace
 * are unjudged because the budget ran out mid-dataset, not because the evidence
 * was weak.
 */
export const STATUS_PHRASE = {
  verified: 'Proven',
  unverified: 'No verdict',
  not_proven: 'No verdict',
  conflicting: 'Disputed',
  judgment_unavailable: 'Not checked yet',
  rate_limited: 'Come back later',
};

/** Why a field is missing, in words. */
export const GAP_PHRASE = {
  schema_gap: 'The sources do not carry it',
  depth_gap: 'Some rows are missing it',
  evidence_gap: 'The values exist, nobody judged them',
};

/** What phase a gap has reached, in words. */
export const GAP_PHASE = {
  0: 'not attempted',
  1: 'stored pages re-read',
  2: 'searched the web',
};

export const GAP_STATE_PHRASE = {
  open: 'still to try',
  resolved: 'filled',
  exhausted: 'the sources do not answer it',
  refused: 'cannot be tried safely',
};

/** A pipeline stage, in words. */
export const STAGE_PHRASE = {
  PLANNING: 'Reading your request',
  DISCOVERING: 'Looking for places to find this',
  FETCHING: 'Reading pages',
  REDUCING: 'Making the text readable',
  EXTRACTING: 'Filling in values',
  VALIDATING: 'Checking every value against the page',
  DEDUPLICATING: 'Removing duplicates',
  FINALIZING: 'Finishing up',
  COMPLETED: 'Done',
  FAILED: 'Did not finish',
  CANCELLED: 'Stopped',
};

/**
 * A run event as one plain line, or `null` when it has no plain reading.
 *
 * `null` is the load-bearing part. Callers skip it, so an event added to the
 * backend without a line here simply does not appear in the running commentary —
 * it stays in the full log behind "Show details" rather than arriving as a
 * snake_case token nobody asked for.
 */
const EVENT_LINE = {
  'source.fetched': (e) => e.message || 'Read a page',
  'source.reused': (e) => e.message || 'Reused a page read earlier',
  'source.discovered': (e) => e.message || 'Found a source',
  'record.extracted': () => 'Extracted a record',
  'record.verified': () => 'Checked a record',
  'record.needs_review': () => 'Found a record worth a look',
  'record.rejected': (e) => e.message || 'Rejected a record',
  'duplicate.merged': (e) => e.message || 'Merged duplicates',
  'run.partial': (e) => e.message || 'Ran out of budget and kept what it had',
  'run.completed': () => 'Done',
  'run.failed': (e) => e.message || 'Did not finish',
  'run.cancelled': () => 'Stopped',
};

const STAGE_LINE = {
  PLANNING: 'Reading your request',
  DISCOVERING: 'Looking for places to find this',
  FETCHING: 'Reading pages',
  REDUCING: 'Making the text readable',
  EXTRACTING: 'Filling in values',
  VALIDATING: 'Checking every value against the page',
  DEDUPLICATING: 'Removing duplicates',
  FINALIZING: 'Finishing up',
};

export function friendlyLine(event) {
  if (!event) return null;
  if (event.type === 'stage.started' || event.type === 'stage.progress') {
    return STAGE_LINE[event.stage] || null;
  }
  const fn = EVENT_LINE[event.type];
  return fn ? fn(event) : null;
}

/** The most recent event that has a plain reading, for a one-line summary. */
export function latestFriendly(events) {
  if (!Array.isArray(events)) return null;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const line = friendlyLine(events[i]);
    if (line) return line;
  }
  return null;
}
