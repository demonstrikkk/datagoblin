/**
 * Single source of truth for how the backend's vocabulary is *shown*.
 *
 * Every status, stage and event type the API can emit is mapped here, so a
 * colour or label is never invented at the call site and a missing value
 * degrades to a neutral "unknown" instead of a wrong confident colour.
 */

/**
 * Terminal run statuses — a run in one of these will never change again.
 * `'FAILED+partial'` is `runStatusKey`'s composed form and must be included,
 * or a partial run is simultaneously classified as finished and still
 * running, which rendered as three contradictory pills at once.
 */
export const TERMINAL_STATUS = new Set([
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'PARTIAL',
  'FAILED+partial',
]);

export const RUN_STATUS = {
  PLANNING:  { label: 'Planning',   tone: 'muted',  glyph: '·' },
  RUNNING:   { label: 'Running',    tone: 'info',   glyph: '◍', live: true },
  COMPLETED: { label: 'Complete',   tone: 'ok',     glyph: '✓' },
  FAILED:    { label: 'Failed',     tone: 'danger', glyph: '✕' },
  CANCELLED: { label: 'Cancelled',  tone: 'muted',  glyph: '⊘' },
  PARTIAL:   { label: 'Partial',    tone: 'warn',   glyph: '◐' },
  // A partial result is persisted as FAILED + partial=true, and the stage is
  // FAILED while the status is PARTIAL. Surface the truth, not the raw enum.
  'FAILED+partial': { label: 'Partial (error)', tone: 'warn', glyph: '◐' },
};

/** Normalise the two ways the API reports a partial run into one key. */
export function runStatusKey(run) {
  if (!run) return null;
  if (run.status === 'PARTIAL') return 'PARTIAL';
  if (run.status === 'FAILED' && run.partial) return 'FAILED+partial';
  return run.status;
}

export const runStatusMeta = (run) => {
  const key = runStatusKey(run);
  return RUN_STATUS[key] || { label: key || 'Unknown', tone: 'muted', glyph: '?' };
};

/**
 * What each verification status actually means, in the words a reviewer needs.
 *
 * `verified` deliberately does NOT say "the judge agreed". Per docs/11 the
 * validator short-circuits on verbatim containment — when the value appears
 * inside its own quote there is nothing left to judge, so the judge is never
 * called. That is the common path, and describing it as judge-confirmed
 * overstated the strongest claim the product makes. A technically-literate
 * buyer will find that gap in the first demo.
 */
export const REC_VERIFY = {
  verified: {
    label: 'Verified',
    tone: 'ok',
    glyph: '✓',
    hint: 'The value appears verbatim in its own quote, and that quote re-locates in the stored page text at the cited offsets.',
  },
  unverified: {
    label: 'Unverified',
    tone: 'muted',
    glyph: '?',
    hint: 'No evidence check passed. Not a claim of correctness, and not a claim it is wrong.',
  },
  conflicting: {
    label: 'Conflicting',
    tone: 'danger',
    glyph: '≠',
    hint: 'Sources or the judge disagree about this value.',
  },
  judgment_unavailable: {
    label: 'No judgment',
    tone: 'info',
    glyph: '–',
    hint: 'The quote was found in the stored page, but no judge was reachable to rule on it. Deliberately not counted as verified.',
  },
  rate_limited: {
    label: 'Rate limited',
    tone: 'judge',
    glyph: '⏱',
    hint: 'The judge provider throttled the request. An operational condition worth retrying, not an absence of evidence.',
  },
  rejected: {
    label: 'Rejected',
    tone: 'danger',
    glyph: '✕',
    hint: 'Failed schema, type or support validation. Dropped rather than softened.',
  },
  needs_review: {
    label: 'Needs review',
    tone: 'warn',
    glyph: '!',
    hint: 'A human should look at this record.',
  },
};

export const SOURCE_STATUS = {
  ok:      { label: 'Fetched',  tone: 'ok' },
  failed:  { label: 'Failed',   tone: 'danger' },
  skipped: { label: 'Skipped',  tone: 'muted' },
  blocked: { label: 'Blocked',  tone: 'warn' },
};

export const TONE = {
  ok:     'text-ok bg-ok-soft border-ok/25',
  warn:   'text-warn bg-warn-soft border-warn/25',
  danger: 'text-danger bg-danger-soft border-danger/25',
  info:   'text-info bg-info-soft border-info/25',
  judge:  'text-judge bg-judge-soft border-judge/25',
  muted:  'text-muted bg-warm/70 border-rule-2',
  accent: 'text-accent bg-warn-soft border-accent/30',
};

export const toneClass = (tone) => TONE[tone] || TONE.muted;

/**
 * The `RunStage` values, in the order the runner executes them.
 * Keys are the literal enum values from `app/core/constants.py` — no mapping
 * layer, so a stage name can never drift from the backend.
 *
 * Only the eight *work* stages are listed. `COMPLETED`, `FAILED` and
 * `CANCELLED` are run *statuses*, not steps, and rendering "Complete" as the
 * last node of a progress rail made a running job readable as a finished one.
 * The status pill is the single place a terminal state appears.
 *
 * `REDUCING` and `FINALIZING` are declared but never emitted, so the rail draws
 * them dimmed rather than pretending they ran.
 */
export const STAGES = [
  { key: 'PLANNING',      label: 'Plan',       emits: true  },
  { key: 'DISCOVERING',   label: 'Discover',   emits: true  },
  { key: 'FETCHING',      label: 'Fetch',      emits: true  },
  { key: 'REDUCING',      label: 'Reduce',     emits: false },
  { key: 'EXTRACTING',    label: 'Extract',    emits: true  },
  { key: 'VALIDATING',    label: 'Validate',   emits: true  },
  { key: 'DEDUPLICATING', label: 'Deduplicate',emits: true  },
  { key: 'FINALIZING',    label: 'Finalize',   emits: false },
];

/** Statuses that mean the run has stopped, whatever the last stage was. */
export const TERMINAL_STAGE_VALUES = ['COMPLETED', 'FAILED', 'CANCELLED'];

/**
 * A run whose status is still the initial `PLANNING` but whose stage has
 * advanced is, in substance, running. The API sets `status: "PLANNING"` at
 * creation and only rewrites it on a terminal event, so the stage is the more
 * truthful signal and the pill should say so.
 */
export function isEffectivelyRunning(...runs) {
  for (const r of runs) {
    if (!r) continue;
    if (TERMINAL_STATUS.has(runStatusKey(r))) continue;
    if (r.partial === true) continue;
    const stage = r.current_stage || r.status;
    if (stage && stage !== 'PLANNING' && !TERMINAL_STAGE_VALUES.includes(stage)) return true;
  }
  return false;
}

export const STAGE_INDEX = Object.fromEntries(STAGES.map((s, i) => [s.key, i]));

/** 13 declared event types -> human phrasing. */
export const EVENT_META = {
  'run.partial':        { tone: 'warn',   label: 'run partial',      verb: 'reported a partial run' },
  'stage.started':      { tone: 'info',   label: 'stage',            verb: 'started' },
  'stage.progress':     { tone: 'muted',  label: 'progress',         verb: 'progressed' },
  'source.discovered':  { tone: 'muted',  label: 'source',           verb: 'discovered' },
  'source.fetched':     { tone: 'ok',     label: 'source',           verb: 'fetched' },
  'record.extracted':   { tone: 'info',   label: 'record',           verb: 'extracted' },
  'record.verified':    { tone: 'ok',     label: 'record',           verb: 'verified' },
  'record.needs_review':{ tone: 'warn',   label: 'record',           verb: 'flagged for review' },
  'record.rejected':    { tone: 'danger', label: 'record',           verb: 'rejected' },
  'duplicate.merged':   { tone: 'judge',  label: 'duplicate',        verb: 'merged' },
  'run.completed':      { tone: 'ok',     label: 'run',              verb: 'completed' },
  'run.failed':         { tone: 'danger', label: 'run',              verb: 'failed' },
  'run.cancelled':      { tone: 'muted',  label: 'run',              verb: 'was cancelled' },
};

/* ------------------------------------------------------------------ format */

export function num(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US');
}

export function pct(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `${(Number(n) * 100).toFixed(digits)}%`;
}

/** compact 12.4k / 1.2M */
export function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const v = Number(n);
  if (Math.abs(v) < 1000) return String(v);
  if (Math.abs(v) < 1e6) return `${(v / 1000).toFixed(v < 10000 ? 1 : 0)}k`;
  return `${(v / 1e6).toFixed(1)}M`;
}

export function bytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}

export function ms(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const v = Number(n);
  if (v < 1000) return `${Math.round(v)}ms`;
  if (v < 60_000) return `${(v / 1000).toFixed(1)}s`;
  const m = Math.floor(v / 60_000);
  const s = Math.round((v % 60_000) / 1000);
  return `${m}m ${s.toString().padStart(2, '0')}s`;
}

export function when(iso, { relative = true } = {}) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  if (!relative) {
    return d.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }
  const diff = Date.now() - d.getTime();
  const abs = Math.abs(diff);
  const future = diff < 0;
  const units = [
    [1000, 'just now', null],
    [60_000, 's', 1000],
    [3_600_000, 'm', 60_000],
    [86_400_000, 'h', 3_600_000],
    [604_800_000, 'd', 86_400_000],
  ];
  if (abs < 45_000) return future ? 'in a moment' : 'just now';
  let label = null;
  for (const [limit, suffix, div] of units) {
    if (abs < limit) {
      label = suffix === null ? 'just now' : `${Math.round(abs / div)}${suffix}`;
      break;
    }
  }
  if (!label) label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return future ? `in ${label}` : `${label} ago`;
}

export function duration(startIso, endIso) {
  if (!startIso) return '—';
  const a = new Date(startIso).getTime();
  const b = endIso ? new Date(endIso).getTime() : Date.now();
  if (Number.isNaN(a) || Number.isNaN(b)) return '—';
  return ms(Math.max(0, b - a));
}

export function host(url) {
  if (!url) return '—';
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return String(url).slice(0, 48);
  }
}

export function truncate(s, n = 80) {
  if (!s) return '';
  const t = String(s);
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

/**
 * Run identity.
 *
 * Two shapes exist for the same concept. `GET /runs/{id}` (the live view) uses
 * `run_id`; `GET /history` and the stored row use `id`. Reading only one of
 * them silently produced blank identifiers in the run list.
 */
export const runId = (run) => run?.run_id || run?.id || '';

/**
 * Normalise the run counters, choosing the richest source available.
 *
 * There are three shapes in play for the same run, and they disagree:
 *  - live in-memory run:  `counters` with 9 field-level keys
 *  - stored run, read via `GET /runs/{id}` after eviction: `counters` with the
 *    legacy coarse keys only (`records`, `verified`, `needs_review`)
 *  - `GET /history` and the stored row: `stats.counts` with all 15 keys
 *
 * Passing several candidates returns whichever actually carries field-level
 * detail, so a run view does not silently lose its quality breakdown just
 * because the live endpoint fell back to the flattened row.
 *
 * Always returns an object, never null. A run that was cancelled before it
 * wrote any stats has no counters at all, and returning null here meant every
 * caller that did `runCounters(r).fields_verified` crashed the whole view.
 * Use `hasCounters()` when "no data" and "empty data" must be distinguished.
 */
const FIELD_LEVEL_KEYS = [
  'fields_verified',
  'fields_unverified',
  'fields_conflicting',
  'fields_judgment_unavailable',
  'fields_rate_limited',
  'records_fully_verified',
];

function candidateCounters(run) {
  if (!run || typeof run !== 'object') return null;
  return run.counters || run.stats?.counts || null;
}

export function runCounters(...runs) {
  let fallback = null;
  for (const r of runs) {
    const c = candidateCounters(r);
    if (!c) continue;
    if (FIELD_LEVEL_KEYS.some((k) => k in c)) return c; // richest wins immediately
    if (!fallback) fallback = c;
  }
  return fallback || {};
}

/** Does any of these runs carry counters at all? */
export function hasCounters(...runs) {
  return runs.some((r) => candidateCounters(r) !== null);
}

/** Does this run have real field-level verification detail? */
export function hasFieldLevelDetail(...runs) {
  const c = runCounters(...runs);
  return FIELD_LEVEL_KEYS.some((k) => k in c);
}

/** `GET /history` carries the record total twice; prefer the explicit one. */
export function runRecordTotal(...runs) {
  for (const r of runs) {
    const c = candidateCounters(r);
    if (!c) continue;
    if (c.records !== undefined && c.records !== null && c.records !== '') {
      return Number(c.records) || 0;
    }
  }
  for (const r of runs) {
    const n = Number(r?.stats?.records);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return 0;
}

/**
 * Did this run store a partial result?
 *
 * A partial run is reported three ways depending on the endpoint: `status` is
 * `PARTIAL`, or the status is `FAILED` with `partial: true`, or the counters
 * carry `partial`. All three are checked, because relying on the flag alone
 * missed runs that are plainly partial.
 */
export function isPartialRun(...runs) {
  return runs.some(
    (r) =>
      r &&
      (r.partial === true ||
        r.status === 'PARTIAL' ||
        candidateCounters(r)?.partial === true ||
        (r.status === 'FAILED' && candidateCounters(r)?.partial === true))
  );
}

/**
 * Read the verification breakdown off a run.
 *
 * Keys come from the real payloads:
 *   fields_verified, fields_unverified, fields_conflicting,
 *   fields_judgment_unavailable, fields_rate_limited
 * plus the stricter record-level counts `records_fully_verified` and
 * `records_needing_review`.
 *
 * `records_fully_verified` means *every field on the record* was verified,
 * which is a deliberately stronger claim than "some fields were verified".
 * The UI must not conflate the two.
 */
export function verifySummary(...runs) {
  if (!hasCounters(...runs)) return null;
  const c = runCounters(...runs);

  const fieldsVerified = Number(c.fields_verified) || 0;
  const fieldsUnverified = Number(c.fields_unverified) || 0;
  const fieldsConflicting = Number(c.fields_conflicting) || 0;
  const fieldsUnjudged = Number(c.fields_judgment_unavailable) || 0;
  const fieldsThrottled = Number(c.fields_rate_limited) || 0;

  const rows = [];
  if (fieldsVerified) rows.push({ key: 'verified', n: fieldsVerified, ...REC_VERIFY.verified });
  if (fieldsUnverified) rows.push({ key: 'unverified', n: fieldsUnverified, ...REC_VERIFY.unverified });
  if (fieldsConflicting) rows.push({ key: 'conflicting', n: fieldsConflicting, ...REC_VERIFY.conflicting });
  if (fieldsUnjudged)
    rows.push({ key: 'judgment_unavailable', n: fieldsUnjudged, ...REC_VERIFY.judgment_unavailable });
  if (fieldsThrottled)
    rows.push({ key: 'rate_limited', n: fieldsThrottled, ...REC_VERIFY.rate_limited });

  const total =
    fieldsVerified + fieldsUnverified + fieldsConflicting + fieldsUnjudged + fieldsThrottled;

  if (!total) return null;

  return {
    rows,
    total,
    fieldTotal: total,
    recordLevel: false,
    provenShare: total ? fieldsVerified / total : 0,
    fullyVerified: Number(c.records_fully_verified) || 0,
    needingReview: Number(c.records_needing_review) || 0,
    records: Number(c.records) || 0,
  };
}

// -- Ask result charts -------------------------------------------------------
// These two decide what a chart shows, so they are here with the other
// formatting helpers rather than inside a component: a wrong decision draws a
// confident, meaningless picture, and that should be checkable without a
// browser.

/** Fraction of a column's non-empty values that parse as numbers. */
export function numericShare(rows, col) {
  const seen = (rows || [])
    .map((r) => r?.[col])
    .filter((v) => v !== null && v !== undefined && v !== '');
  if (!seen.length) return 0;
  const ok = seen.filter((v) =>
    Number.isFinite(Number(String(v).replace(/[,$%\s]/g, '')))).length;
  return ok / seen.length;
}

/** The most frequent distinct values of a column, biggest first.

 * Empty below two distinct values: one category is not a distribution, and a
 * bar chart of a single full-width bar says nothing a sentence would not. */
export function topCategories(rows, col, limit = 8) {
  const counts = new Map();
  for (const r of rows || []) {
    const v = r?.[col];
    if (v === null || v === undefined || v === '') continue;
    const key = String(v);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  if (counts.size < 2) return [];
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit);
}