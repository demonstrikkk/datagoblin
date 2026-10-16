/**
 * The dataset view model — the seam between the API and the workspace UI.
 *
 * WHY THIS FILE EXISTS
 *
 * The dataset workspace is a presentation-layer transformation. Every number it
 * shows already exists in some API response; nothing here fetches, and nothing
 * here may invent. This module is the only place that knows how those responses
 * are shaped, so when a payload changes the fix lands in one file rather than in
 * eleven components.
 *
 * It is deliberately plain ESM with no JSX and no React import, for two reasons:
 * it can be exercised by `node test/dataset-view-model.test.mjs` without a
 * browser, and a derivation with no view attached is much easier to argue about.
 *
 * THE ONE RULE THAT GOVERNS THIS FILE
 *
 *     A measurement that was not taken is reported as "not counted",
 *     never as zero, and never as a percentage.
 *
 * Several runs in this database wrote no aggregate counters while still storing
 * per-field status. Rendering those as `0% proven` is a confident negative the
 * data does not support — it is the exact failure this product exists to
 * prevent. Every derived figure therefore carries a `counted` flag, and every
 * surface that renders one checks it.
 */

/* ------------------------------------------------------------------ helpers */

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Counters arrive as numbers or as numeric strings depending on the run. */
const n = (v) => {
  if (isNum(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const parsed = Number(v);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
};

/** Thousands separators. The workspace shows counts in the thousands often. */
const fmt = (v) => (n(v) === null ? '—' : n(v).toLocaleString('en-US'));

const pct = (part, whole) => {
  const a = n(part);
  const b = n(whole);
  if (a === null || b === null || b <= 0) return null;
  return a / b;
};

/** Percentage as an integer, or null. Never substitutes 0 for "unknown". */
const pctInt = (part, whole) => {
  const r = pct(part, whole);
  return r === null ? null : Math.round(r * 100);
};

/**
 * Two generations of `counts` coexist in this database.
 *
 * Current runs write field-level detail (`fields_verified`,
 * `fields_judgment_unavailable`, `fields_rate_limited`,
 * `records_fully_verified`). Older datasets only carry the coarse shape
 * (`records`, `verified`, `needs_review`). The generation is detected rather
 * than assumed, because assuming the modern one renders an old dataset's
 * missing counters as a quality failure that never happened.
 */
export function countersOf(dataset) {
  const c = (dataset && dataset.counts) || {};
  const modern = 'fields_verified' in c || 'records_fully_verified' in c;
  const proven = n(c.fields_verified) ?? 0;
  const unjudged = n(c.fields_judgment_unavailable) ?? 0;
  const throttled = n(c.fields_rate_limited) ?? 0;
  const conflicting = n(c.fields_conflicting) ?? 0;
  const fieldTotal = proven + unjudged + throttled + conflicting;
  return {
    raw: c,
    modern,
    proven,
    unjudged,
    throttled,
    conflicting,
    fieldTotal,
    /** False when this run wrote no field-level counters at all. */
    counted: modern && fieldTotal > 0,
    records: n(c.records),
    fullyVerified: modern ? n(c.records_fully_verified) : null,
    needingReview: modern ? n(c.records_needing_review) : null,
    legacyVerified: modern ? null : n(c.verified),
    legacyNeedsReview: modern ? null : n(c.needs_review),
  };
}

/* ------------------------------------------------------------------- health */

/**
 * Freshness, from the retrieval time on the stored pages.
 *
 * This is the one health signal that is genuinely a property of the *pages*
 * rather than of the extraction, so it is computed here rather than taken from
 * a counter. A dataset with no recorded retrieval times reports "not counted"
 * — which is different from a dataset whose sources are all old.
 */
export function freshnessOf(sources) {
  const rows = sources || [];
  const ages = [];
  rows.forEach((s) => {
    const at = s && (s.retrieved_at || s.fetched_at);
    if (!at) return;
    const t = new Date(at).getTime();
    if (Number.isFinite(t)) ages.push(Date.now() - t);
  });
  if (!ages.length) {
    return {
      counted: false,
      share: null,
      recentCount: 0,
      medianAgeMs: null,
      oldestAgeMs: null,
      pages: rows.length,
      dated: 0,
    };
  }
  ages.sort((a, b) => a - b);
  const withinDay = ages.filter((a) => a <= 86_400_000).length;
  return {
    counted: true,
    // Share of pages retrieved in the last day. A defensible reading of "how
    // much of this dataset was gathered recently", and one that can be stated as
    // a numerator and a denominator rather than as a vibe.
    share: pct(withinDay, ages.length),
    recentCount: withinDay,
    medianAgeMs: ages[Math.floor(ages.length / 2)],
    oldestAgeMs: ages[ages.length - 1],
    pages: rows.length,
    dated: ages.length,
  };
}

/**
 * The four health signals.
 *
 * Each one is a *measurement with its denominator attached*, because a bare
 * percentage invites the reader to treat it as a score. `detail` is the sentence
 * that must accompany the number; the UI renders it rather than inventing one.
 *
 * `target` is the rail section each signal drills into, so a health figure is a
 * way into the workspace rather than a decoration.
 */
export function healthOf({ dataset, profile, coverage, counters, sources }) {
  const covFields = (coverage && coverage.fields) || [];
  const profFields = (profile && profile.fields) || [];

  /*
   * Coverage — how many cells hold a value at all.
   *
   * `coverage.fields` is preferred because it scans every record;
   * `profile.fields` is a fallback and may be sampled, so its own
   * `records_read` is carried through to say so.
   */
  let coveragePct = null;
  let coverageDetail = 'No coverage was recorded for this dataset.';
  let coverageCells = null;
  if (covFields.length) {
    const present = covFields.reduce((a, f) => a + (n(f.present) ?? 0), 0);
    const records = covFields.reduce((a, f) => a + (n(f.records) ?? 0), 0);
    coverageCells = { present, records };
    coveragePct = pctInt(present, records);
    coverageDetail = `${fmt(present)} of ${fmt(records)} declared cells hold a value.`;
  } else if (profFields.length) {
    const present = profFields.reduce((a, f) => a + (n(f.trust && f.trust.present) ?? 0), 0);
    const records = profFields.reduce((a, f) => a + (n(f.records) ?? 0), 0);
    coverageCells = { present, records };
    coveragePct = pctInt(present, records);
    coverageDetail = `${fmt(present)} of ${fmt(records)} declared cells hold a value${
      profile.sampled ? `, read from the first ${fmt(profile.records_read)}` : ''
    }.`;
  }

  /*
   * Evidence — of the cells that hold a value, how many carry a verdict.
   *
   * Denominator is filled cells, not all cells. "76% of filled values are
   * proven" and "76% of all cells are proven" are different claims and only the
   * first is true of a dataset with gaps.
   */
  let evidencePct = null;
  let evidenceDetail = 'No field-level verification was recorded for this run.';
  if (counters.counted) {
    evidencePct = pctInt(counters.proven, counters.fieldTotal);
    evidenceDetail = `${fmt(counters.proven)} of ${fmt(counters.fieldTotal)} extracted values carry judge-verified quotes.`;
  }

  /*
   * Consistency — how often sources disagree.
   *
   * Derived from the same counters rather than from the conflict list, because
   * a conflict count alone cannot be turned into a rate without a denominator.
   */
  let consistencyPct = null;
  let consistencyDetail = 'No source-agreement count was recorded for this run.';
  if (counters.counted && counters.fieldTotal > 0) {
    consistencyPct = pctInt(counters.fieldTotal - counters.conflicting, counters.fieldTotal);
    const openConflicts = (coverage && n(coverage.backlog && coverage.backlog.open_conflicts)) ?? null;
    consistencyDetail =
      `${fmt(counters.conflicting)} of ${fmt(counters.fieldTotal)} extracted values were disputed by another source` +
      (openConflicts === null ? '.' : `, and ${fmt(openConflicts)} conflict${openConflicts === 1 ? '' : 's'} are still open.`);
  }

  const fresh = freshnessOf(sources);
  const freshnessPct = fresh.counted ? pctInt(fresh.recentCount, fresh.dated) : null;

  return {
    coverage: {
      key: 'coverage',
      label: 'Coverage',
      value: coveragePct,
      counted: coveragePct !== null,
      tone: coveragePct === null ? 'muted' : coveragePct >= 90 ? 'ok' : coveragePct >= 70 ? 'warn' : 'danger',
      detail: coverageDetail,
      target: 'coverage',
    },
    evidence: {
      key: 'evidence',
      label: 'Evidence',
      value: evidencePct,
      counted: evidencePct !== null,
      tone: evidencePct === null ? 'muted' : evidencePct >= 90 ? 'ok' : evidencePct >= 60 ? 'warn' : 'danger',
      detail: evidenceDetail,
      target: 'review',
    },
    freshness: {
      key: 'freshness',
      label: 'Freshness',
      value: freshnessPct,
      counted: freshnessPct !== null,
      tone: freshnessPct === null ? 'muted' : 'ok',
      detail: fresh.counted
        ? `${fmt(fresh.dated)} of ${fmt(fresh.pages)} stored pages were retrieved in the last day.`
        : 'No retrieval time was recorded against these sources.',
      target: 'sources',
    },
    consistency: {
      key: 'consistency',
      label: 'Consistency',
      value: consistencyPct,
      counted: consistencyPct !== null,
      tone: consistencyPct === null ? 'muted' : consistencyPct >= 95 ? 'ok' : consistencyPct >= 85 ? 'warn' : 'danger',
      detail: consistencyDetail,
      target: 'conflicts',
    },
  };
}

/* --------------------------------------------------------------- the shapes */

/**
 * Which representation a field can honestly support.
 *
 * This is the schema-driven dispatch the visualisation layer is built on. The
 * profiler has already decided the field's `kind` from its values; the rules
 * below only decide whether that kind can be *drawn*.
 *
 * Every refusal here is a refusal to imply a shape the data does not have:
 *
 *   - `empty`   — the schema asks for it and no record has it. Described.
 *   - `scalar`  — one distinct value. A one-bar chart is a sentence with axes.
 *   - `numeric` — needs at least two buckets, or it is a constant.
 *   - `date`    — needs at least two years, or it is a point in time.
 *   - `categorical` — needs at least two distinct values, or it is a constant.
 *
 * Returns null for anything that cannot be drawn, so a caller cannot render an
 * empty chart frame.
 */
export function shapeFor(field) {
  if (!field) return null;
  const kind = field.kind;
  const shape = field.shape || {};

  if (kind === 'empty') return { kind: 'empty', why: 'declared by the schema, filled on no record' };
  if (kind === 'scalar') return { kind: 'scalar', why: 'one distinct value across every record' };

  if (kind === 'date') {
    const years = shape.by_year || [];
    if (years.length < 2) return { kind: 'point', why: 'a single date, so there is no span to draw' };
    return {
      kind: 'timeline',
      byYear: years,
      min: shape.min,
      max: shape.max,
      // Empty years are kept by the widget, not filtered here: a gap in a date
      // column is a finding.
    };
  }

  if (kind === 'numeric') {
    const buckets = shape.buckets || [];
    if (buckets.length < 2) return { kind: 'point', why: 'one value, so there is no spread to draw' };
    return { kind: 'histogram', buckets, median: shape.median };
  }

  if (kind === 'categorical') {
    const values = shape.values || [];
    if (values.length < 2) return { kind: 'point', why: 'one distinct value, so there is no distribution' };
    return {
      kind: 'rank',
      values,
      truncated: Boolean(shape.truncated),
      total: values.reduce((a, v) => a + (n(v.n) ?? 0), 0),
    };
  }

  return null;
}

/**
 * The fields worth drawing on an overview, best first.
 *
 * Ordering is by how much a field tells a reader who has never seen the
 * dataset: fields that are filled on most records and have a real shape come
 * first, fields that are mostly empty come last, and fields that cannot be
 * drawn are dropped entirely rather than shown as an empty frame.
 *
 * `limit` keeps the overview to a scannable strip. The Fields section is where
 * all of them live.
 */
export function topShapes(fields, limit = 4) {
  const drawable = (fields || [])
    .map((f) => {
      const shape = shapeFor(f);
      if (!shape || shape.kind === 'empty' || shape.kind === 'scalar' || shape.kind === 'point') {
        return null;
      }
      const trust = f.trust || {};
      const present = n(trust.present) ?? 0;
      const records = n(f.records) ?? 0;
      return { field: f, shape, fill: pct(present, records) ?? 0, records };
    })
    .filter(Boolean);

  // Most-filled first; a tie falls back to the order the schema declared, which
  // is the reader's own mental ordering of the fields.
  drawable.sort((a, b) => b.fill - a.fill);
  return drawable.slice(0, limit);
}

/* ------------------------------------------------------------------ findings */

/**
 * What the run actually found — every line traceable to a stored number.
 *
 * This replaces the "AI insights" panel the brief rejected, and the difference
 * is not stylistic. An insight box asserts conclusions from a language model;
 * each line below is arithmetic over counts the backend measured, and each
 * carries the sentence that proves it. Nothing here is a prediction, a
 * recommendation, or a comparison against a previous collection — this dataset
 * page has no access to a previous run's numbers, and inventing a delta would
 * be the single most dishonest thing on the screen.
 */
export function findingsOf({ dataset, profile, coverage, gaps, conflicts, counters, shapes }) {
  const out = [];
  const covFields = (coverage && coverage.fields) || [];
  const profFields = (profile && profile.fields) || [];

  /* Repeats across sources — the finding that most justifies the whole product. */
  const place = profile && profile.place;
  if (place && (place.values || []).length > 1) {
    const total = place.values.reduce((a, v) => a + (n(v.n) ?? 0), 0);
    const top = place.values[0];
    if (top && total > 0) {
      out.push({
        id: 'concentration',
        tone: 'muted',
        lead: place.values.length > 1 ? 'These records are not evenly spread' : null,
        text: `${fmt(top.n)} of ${fmt(total)} records sit under one value of ${
          place.field
        } — ${Math.round((top.n / total) * 100)}% of everything collected.`,
        detail: place.variants
          ? `${fmt(place.as_written_count)} distinct spellings were collapsed into ${fmt(
              place.variants
            )} by first segment. That merging is a rule, not a lookup.`
          : null,
        target: 'fields',
      });
    }
  }

  /* Which fields are thin, worst first, with the cause named. */
  if (covFields.length) {
    const thin = covFields
      .map((f) => ({ f, present: n(f.present) ?? 0, records: n(f.records) ?? 0 }))
      .filter((x) => x.records > 0)
      .sort((a, b) => a.present / a.records - b.present / b.records);
    if (thin.length && thin[0].present < thin[0].records) {
      const w = thin[0];
      const pctv = Math.round((w.present / w.records) * 100);
      out.push({
        id: 'thinnest-field',
        tone: pctv >= 50 ? 'warn' : 'danger',
        text: `${w.f.field} is the thinnest field — filled on ${fmt(w.present)} of ${fmt(
          w.records
        )} records (${pctv}%).`,
        detail:
          w.present === 0
            ? 'No stored page carried it. Re-reading the same pages will not help; the schema is asking for something these sources do not publish.'
            : 'Filled on some records and empty on others, which is a top-up rather than a schema problem.',
        target: 'coverage',
      });
    }
  }

  /* Provenance strength, as a measurement rather than a score. */
  if (counters.counted) {
    const pctv = Math.round((counters.proven / counters.fieldTotal) * 100);
    out.push({
      id: 'evidence-strength',
      tone: pctv >= 80 ? 'ok' : pctv >= 50 ? 'warn' : 'danger',
      text: `${fmt(counters.proven)} of ${fmt(counters.fieldTotal)} extracted values (${pctv}%) carry a quote that re-locates in the stored page.`,
      detail:
        counters.unjudged || counters.throttled
          ? `${fmt(counters.unjudged)} had no judge reachable and ${fmt(
              counters.throttled
            )} were throttled. Both are operational conditions, not verdicts.`
          : null,
      target: 'review',
    });
  }

  /* Open disputes. */
  const openConflicts =
    (coverage && n(coverage.backlog && coverage.backlog.open_conflicts)) ??
    (conflicts && Array.isArray(conflicts.conflicts) ? conflicts.conflicts.length : null);
  if (openConflicts) {
    out.push({
      id: 'open-conflicts',
      tone: 'danger',
      text: `${fmt(openConflicts)} cell${openConflicts === 1 ? '' : 's'} are recorded twice with different values.`,
      detail: 'Both sides kept their quotes. Resolution is a choice between recorded values — there is no way to type in one that was never extracted.',
      target: 'conflicts',
    });
  }

  /* Outstanding work, as counts rather than as a score. */
  const gapList = (gaps && gaps.gaps) || [];
  const attemptable = gapList.filter((g) => g.attemptable).length;
  if (gapList.length) {
    out.push({
      id: 'gaps',
      tone: 'warn',
      text: `${fmt(gapList.length)} gap${gapList.length === 1 ? '' : 's'} tracked against this dataset${
        attemptable ? `, ${fmt(attemptable)} of which can be attempted from pages already stored` : ''
      }.`,
      detail: 'A gap that no stored page can fill is a schema problem, not a re-run problem.',
      target: 'coverage',
    });
  }

  /* A field that was never filled at all is a different statement, and it is
     the one most worth making loudly. */
  const never = profFields.filter((f) => f.never_extracted).map((f) => f.field);
  if (never.length) {
    out.push({
      id: 'never-extracted',
      tone: 'warn',
      text: `${never.length} declared field${never.length === 1 ? '' : 's'} ${
        never.length === 1 ? 'was' : 'were'
      } never extracted: ${never.slice(0, 4).join(', ')}${never.length > 4 ? `, +${never.length - 4} more` : ''}.`,
      detail: 'Nothing in storage can settle these. Learn selectors, or drop them from the schema.',
      target: 'fields',
    });
  }

  return out;
}

/* ---------------------------------------------------------------- the model */

/**
 * Build the whole workspace model from whatever has arrived so far.
 *
 * Every argument is optional and every one of them may be `undefined` while its
 * request is still in flight. The model therefore degrades section by section
 * rather than throwing, which is what lets the workspace paint a header and a
 * record table immediately and fill in the analysis as the slower reads land.
 */
export function datasetViewModel({
  dataset = null,
  profile = null,
  coverage = null,
  gaps = null,
  conflicts = null,
  sources = null,
} = {}) {
  const counters = countersOf(dataset);
  const sourceRows = (sources && sources.sources) || [];
  const health = healthOf({ dataset, profile, coverage, counters, sources: sourceRows });
  const profFields = (profile && profile.fields) || [];
  const shapes = topShapes(profFields);

  const fieldCount =
    (dataset && Array.isArray(dataset.schema) && dataset.schema.length) ||
    profFields.length ||
    0;

  return {
    identity: {
      id: (dataset && dataset.id) || null,
      name: (dataset && dataset.name) || null,
      runId: (dataset && dataset.run_id) || null,
      createdAt: (dataset && dataset.created_at) || null,
      recordCount: (dataset && n(dataset.record_count)) ?? null,
      fieldCount,
    },

    summary: {
      records: (dataset && n(dataset.record_count)) ?? null,
      fields: fieldCount,
      pages: sourceRows.length,
      sourcesAttempted: (sources && n(sources.sources_attempted)) ?? null,
      sourcesFailed: (sources && n(sources.sources_failed)) ?? null,
      openConflicts:
        (coverage && n(coverage.backlog && coverage.backlog.open_conflicts)) ??
        (conflicts && Array.isArray(conflicts.conflicts) ? conflicts.conflicts.length : 0),
      gaps: (gaps && Array.isArray(gaps.gaps) ? gaps.gaps.length : null) ?? null,
      fullyVerified: counters.fullyVerified,
      needingReview: counters.needingReview,
    },

    counters,

    health,
    freshness: freshnessOf(sourceRows),
    fields: profFields,
    shapes,
    sector: (profile && profile.sector) || null,
    place: (profile && profile.place) || null,

    findings: findingsOf({
      dataset,
      profile,
      coverage,
      gaps,
      conflicts,
      counters,
      shapes,
    }),

    /* Whether the profile was read from every record or a prefix of them.
       Anything drawn from it has to say which. */
    sampled: Boolean(profile && profile.sampled),
    recordsRead: (profile && n(profile.records_read)) ?? null,
  };
}