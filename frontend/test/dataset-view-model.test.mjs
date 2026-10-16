/**
 * The dataset view model, checked against the real module.
 *
 * This is the layer that decides what the workspace is allowed to claim about a
 * dataset, so its failure mode is not a crash — it is a confident, well-formatted
 * number that the stored data does not support. Four large percentages sitting
 * above a record table whose every value carries a real verification status is
 * the most damaging kind of wrong this product could produce, which is why the
 * refusals are asserted here rather than left to review.
 *
 * The refusals under test:
 *
 *   - a measurement that was not taken reports `counted: false`, never 0
 *   - an older run's coarse counters are not read as modern field-level ones
 *   - a field with one value, or none, is not given a drawable shape
 *   - every finding carries the counts it was derived from
 *
 * Run with:  npm run test:viewmodel
 */
import assert from 'node:assert/strict';

import {
  countersOf,
  datasetViewModel,
  freshnessOf,
  healthOf,
  shapeFor,
  topShapes,
} from '../src/lib/datasetViewModel.js';

let failures = 0;

function check(name, fn) {
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failures += 1;
    console.log(`  FAIL  ${name}\n        ${e.message.split('\n')[0]}`);
  }
}

/* ------------------------------------------------------------- fixtures */

const modernDataset = {
  id: 'ds_1',
  run_id: 'run_1',
  name: 'Senior AI roles',
  record_count: 120,
  schema: [{ name: 'company' }, { name: 'role' }, { name: 'location' }],
  created_at: '2026-09-01T00:00:00Z',
  counts: {
    records: 120,
    fields_verified: 912,
    fields_judgment_unavailable: 180,
    fields_rate_limited: 108,
    fields_conflicting: 60,
    records_fully_verified: 24,
  },
};

const legacyDataset = {
  id: 'ds_2',
  name: 'Old run',
  record_count: 40,
  schema: [{ name: 'title' }],
  counts: { records: 40, verified: 12, needs_review: 5 },
};

const profile = {
  dataset_id: 'ds_1',
  records: 120,
  records_read: 120,
  sampled: false,
  totals: { fields: 3, never_extracted: 1, partial: 1, complete: 1, conflicting: 1 },
  place: {
    field: 'location',
    values: [
      { value: 'Delhi', n: 38 },
      { value: 'Remote', n: 31 },
      { value: 'Gurugram', n: 21 },
    ],
    variants: 0,
    as_written_count: 3,
    raw: [],
  },
  sector: {
    sector: 'hr',
    confidence: 0.81,
    method: 'fields',
    matched_fields: [{ field: 'role', score: 0.9 }],
    considered: [],
    floor: 0.4,
  },
  fields: [
    {
      field: 'company',
      records: 120,
      values: 96,
      never_extracted: false,
      fully_filled: true,
      kind: 'categorical',
      trust: { present: 120, missing: 0, verified: 110, unverified: 10, conflicting: 0 },
      shape: { values: [{ value: 'Microsoft', n: 4 }, { value: 'Amazon', n: 3 }] },
    },
    {
      field: 'salary',
      records: 120,
      values: 30,
      never_extracted: false,
      fully_filled: false,
      kind: 'numeric',
      trust: { present: 57, missing: 63, verified: 40, unverified: 17, conflicting: 0 },
      shape: {
        buckets: [
          { from: 0, to: 500000, n: 30 },
          { from: 500000, to: 1000000, n: 27 },
        ],
        median: 620000,
      },
    },
    {
      field: 'source',
      records: 120,
      values: 1,
      never_extracted: false,
      fully_filled: true,
      kind: 'scalar',
      trust: { present: 120, missing: 0, verified: 120, unverified: 0, conflicting: 0 },
      shape: {},
    },
    {
      field: 'notes',
      records: 120,
      values: 0,
      never_extracted: true,
      fully_filled: false,
      kind: 'empty',
      trust: { present: 0, missing: 120, verified: 0, unverified: 0, conflicting: 0 },
      shape: {},
    },
  ],
};

const coverage = {
  records: 120,
  fields: [
    {
      field: 'company',
      records: 120,
      present: 120,
      missing: 0,
      verified: 110,
      unverified: 10,
      conflicting: 0,
      coverage_pct: 100,
      proven_pct: 92,
    },
    {
      field: 'salary',
      records: 120,
      present: 57,
      missing: 63,
      verified: 40,
      unverified: 17,
      conflicting: 0,
      coverage_pct: 48,
      proven_pct: 33,
    },
  ],
  backlog: { items: [], open_conflicts: 3 },
};

const gaps = {
  dataset_id: 'ds_1',
  gaps: [
    {
      id: 'g1',
      field: 'salary',
      category: 'depth_gap',
      records: 120,
      present: 57,
      missing: 63,
      outstanding: 63,
      attemptable: true,
      state: 'open',
    },
  ],
  summary: {},
  tracked: true,
};

const day = 86_400_000;
const now = Date.now();

/* --------------------------------------------------------------- tests */

console.log('=== countersOf: two generations ===');

check('a modern run is detected and its field total excludes nothing', () => {
  const c = countersOf(modernDataset);
  assert.equal(c.modern, true);
  assert.equal(c.counted, true);
  // verified + unjudged + throttled + conflicting
  assert.equal(c.fieldTotal, 912 + 180 + 108 + 60);
  assert.equal(c.fullyVerified, 24);
});

check('a legacy run is NOT read as a modern one', () => {
  const c = countersOf(legacyDataset);
  assert.equal(c.modern, false);
  assert.equal(c.counted, false);
  // The decisive assertion: a run that never counted fields must not report a
  // field total of zero as though it had measured zero verified fields.
  assert.equal(c.fieldTotal, 0);
  assert.equal(c.fullyVerified, null);
  assert.equal(c.legacyVerified, 12);
});

check('a dataset with no counts at all is not counted', () => {
  const c = countersOf({ id: 'x', record_count: 5 });
  assert.equal(c.counted, false);
  assert.equal(c.modern, false);
});

console.log('=== health: an unmeasured signal is never zero ===');

check('coverage is measured from the coverage read', () => {
  const h = healthOf({
    dataset: modernDataset,
    profile,
    coverage,
    counters: countersOf(modernDataset),
    sources: [],
  });
  assert.equal(h.coverage.counted, true);
  // (120 + 57) present of (120 + 120) declared cells
  assert.equal(h.coverage.value, 74);
  assert.match(h.coverage.detail, /177 of 240 declared cells/);
});

check('evidence is a share of filled cells, not of all cells', () => {
  const h = healthOf({
    dataset: modernDataset,
    profile,
    coverage,
    counters: countersOf(modernDataset),
    sources: [],
  });
  // 912 of 1260 extracted values
  assert.equal(h.evidence.value, 72);
  assert.match(h.evidence.detail, /912 of 1,260 extracted values/);
});

check('consistency is a rate, and names the open conflicts', () => {
  const h = healthOf({
    dataset: modernDataset,
    profile,
    coverage,
    counters: countersOf(modernDataset),
    sources: [],
  });
  // (1260 - 60) of 1260
  assert.equal(h.consistency.value, 95);
  assert.match(h.consistency.detail, /3 conflicts are still open/);
});

check('a legacy run reports evidence and consistency as NOT COUNTED', () => {
  const h = healthOf({
    dataset: legacyDataset,
    profile: null,
    coverage: null,
    counters: countersOf(legacyDataset),
    sources: [],
  });
  assert.equal(h.evidence.counted, false);
  assert.equal(h.evidence.value, null);
  assert.equal(h.consistency.counted, false);
  assert.equal(h.consistency.value, null);
  // And the reason is stated rather than implied by a bare zero.
  assert.match(h.evidence.detail, /No field-level verification/);
});

check('no signals are counted when nothing has loaded', () => {
  const h = healthOf({
    dataset: null,
    profile: null,
    coverage: null,
    counters: countersOf(null),
    sources: null,
  });
  for (const key of ['coverage', 'evidence', 'freshness', 'consistency']) {
    assert.equal(h[key].counted, false, `${key} should not be counted`);
    assert.equal(h[key].value, null, `${key} should have no value`);
  }
});

console.log('=== freshness ===');

check('freshness counts only pages that carry a retrieval time', () => {
  const f = freshnessOf([
    { retrieved_at: new Date(now - 1000).toISOString() },
    { retrieved_at: new Date(now - 2000).toISOString() },
    { status: 'ok' }, // no timestamp at all
    { fetched_at: new Date(now - 40 * day).toISOString() },
  ]);
  assert.equal(f.counted, true);
  assert.equal(f.dated, 3);
  assert.equal(f.recentCount, 2);
  assert.equal(f.pages, 4);
});

check('sources with no retrieval time report not counted, not 0%', () => {
  const f = freshnessOf([{ status: 'ok' }, { status: 'failed' }]);
  assert.equal(f.counted, false);
  assert.equal(f.share, null);
});

check('an empty source list is not counted', () => {
  assert.equal(freshnessOf([]).counted, false);
  assert.equal(freshnessOf(null).counted, false);
});

console.log('=== shapeFor: the refusals ===');

check('a categorical field with two values is drawable', () => {
  const s = shapeFor(profile.fields[0]);
  assert.equal(s.kind, 'rank');
  assert.equal(s.values.length, 2);
  assert.equal(s.total, 7);
});

check('a numeric field with two buckets is a histogram with its median', () => {
  const s = shapeFor(profile.fields[1]);
  assert.equal(s.kind, 'histogram');
  assert.equal(s.median, 620000);
});

check('a constant field gets a sentence, never a one-bar chart', () => {
  const s = shapeFor(profile.fields[2]);
  assert.equal(s.kind, 'scalar');
  assert.notEqual(s.kind, 'rank');
  assert.notEqual(s.kind, 'histogram');
});

check('a never-filled field gets a sentence, never a zero-height bar', () => {
  const s = shapeFor(profile.fields[3]);
  assert.equal(s.kind, 'empty');
});

check('a single bucket is a point, not a distribution', () => {
  const s = shapeFor({
    kind: 'numeric',
    shape: { buckets: [{ from: 0, to: 10, n: 5 }], median: 5 },
  });
  assert.equal(s.kind, 'point');
});

check('a single year is a point in time, not a timeline', () => {
  const s = shapeFor({ kind: 'date', shape: { by_year: [{ year: 2025, n: 4 }], min: 2025, max: 2025 } });
  assert.equal(s.kind, 'point');
});

check('empty years are preserved, because a gap is a finding', () => {
  const s = shapeFor({
    kind: 'date',
    shape: {
      by_year: [
        { year: 2022, n: 3 },
        { year: 2023, n: 0 },
        { year: 2024, n: 8 },
      ],
      min: 2022,
      max: 2024,
    },
  });
  assert.equal(s.kind, 'timeline');
  assert.equal(s.byYear.length, 3);
  assert.equal(s.byYear[1].n, 0);
});

check('one distinct category is a point, not a full-width bar', () => {
  const s = shapeFor({ kind: 'categorical', shape: { values: [{ value: 'only', n: 9 }] } });
  assert.equal(s.kind, 'point');
});

check('a missing field shape is refused outright', () => {
  assert.equal(shapeFor(null), null);
  assert.equal(shapeFor({ kind: 'mystery' }), null);
});

console.log('=== topShapes: what earns a panel ===');

check('undrawable fields are dropped rather than shown as empty frames', () => {
  const shapes = topShapes(profile.fields, 4);
  const names = shapes.map((s) => s.field.field);
  assert.ok(names.includes('company'));
  assert.ok(names.includes('salary'));
  assert.ok(!names.includes('source'), 'a constant must not get a panel');
  assert.ok(!names.includes('notes'), 'a never-filled field must not get a panel');
});

check('the best-filled fields come first', () => {
  const shapes = topShapes(profile.fields, 2);
  assert.equal(shapes[0].field.field, 'company');
  assert.ok(shapes[0].fill >= shapes[1].fill);
});

check('the limit is honoured', () => {
  assert.equal(topShapes(profile.fields, 1).length, 1);
});

console.log('=== findings: every line tracesable ===');

check('findings are derived from the payloads, and name their numbers', () => {
  const m = datasetViewModel({
    dataset: modernDataset,
    profile,
    coverage,
    gaps,
    conflicts: null,
    sources: { sources: [{ retrieved_at: new Date(now - 1000).toISOString() }], sources_attempted: 1 },
  });

  const ids = m.findings.map((f) => f.id);
  assert.ok(ids.includes('concentration'));
  assert.ok(ids.includes('thinnest-field'));
  assert.ok(ids.includes('evidence-strength'));
  assert.ok(ids.includes('open-conflicts'));
  assert.ok(ids.includes('gaps'));
  assert.ok(ids.includes('never-extracted'));

  const concentration = m.findings.find((f) => f.id === 'concentration');
  assert.match(concentration.text, /38 of 90 records/);

  const thin = m.findings.find((f) => f.id === 'thinnest-field');
  assert.match(thin.text, /salary is the thinnest field/);
  assert.match(thin.text, /57 of 120/);

  const evidence = m.findings.find((f) => f.id === 'evidence-strength');
  assert.match(evidence.text, /912 of 1,260 extracted values \(72%\)/);
});

check('no finding claims a change against a previous collection', () => {
  // The dataset page has no access to a previous run's numbers. A finding that
  // mentioned one would be fabricated, so the vocabulary is asserted directly.
  //
  // The trend words are matched with their argument rather than bare, because
  // "a top-up" and "close-up" are ordinary English in this UI and a naive
  // /\bup\b/ flags them. A real trend claim always carries a number ("up 8%") or
  // names a comparison ("compared to", "since the last run").
  const m = datasetViewModel({ dataset: modernDataset, profile, coverage, gaps });
  const banned = new RegExp(
    [
      // "up 8%", "down 3%", "rose 12", "fell 4"
      String.raw`\b(up|down|rose|fell|climbed|dropped|grown|grew)\s+[\d.]`,
      String.raw`\b(increase[ds]?|decrease[ds]?|growth|trend|compared\s+(?:to|with)|versus|vs\.?\s)`,
      String.raw`\blast\s+(run|month|week|year|collection|time)\b`,
      String.raw`\b(previous|prior|earlier)\s+(run|collection|dataset|month)\b`,
      String.raw`\bsince\s+(the\s+)?last\b`,
    ].join('|'),
    'i'
  );
  for (const f of m.findings) {
    assert.doesNotMatch(f.text, banned, `finding "${f.id}" implies a trend it cannot have`);
    if (f.detail) assert.doesNotMatch(f.detail, banned, `finding "${f.id}" detail implies a trend`);
  }
});

check('a legacy dataset produces no evidence-strength finding at all', () => {
  const m = datasetViewModel({ dataset: legacyDataset });
  assert.equal(m.findings.find((f) => f.id === 'evidence-strength'), undefined);
});

console.log('=== the model degrades instead of throwing ===');

check('an empty model is valid and reports nothing counted', () => {
  const m = datasetViewModel();
  assert.equal(m.identity.recordCount, null);
  assert.equal(m.findings.length, 0);
  assert.equal(m.shapes.length, 0);
  assert.equal(m.health.evidence.counted, false);
});

check('only the dataset having loaded is enough for identity', () => {
  const m = datasetViewModel({ dataset: modernDataset });
  assert.equal(m.identity.name, 'Senior AI roles');
  assert.equal(m.identity.recordCount, 120);
  assert.equal(m.identity.fieldCount, 3);
  assert.equal(m.sampled, false);
});

check('a sampled profile says so, because a shape is only a sample', () => {
  const m = datasetViewModel({
    dataset: modernDataset,
    profile: { ...profile, sampled: true, records_read: 2000 },
  });
  assert.equal(m.sampled, true);
  assert.equal(m.recordsRead, 2000);
});

check('a null count stays null so the rail renders nothing', () => {
  const m = datasetViewModel({ dataset: modernDataset });
  // Gaps were not read, so the model must not claim there are zero of them.
  assert.equal(m.summary.gaps, null);
});

check('counter strings are coerced, because two run generations both wrote them', () => {
  const c = countersOf({
    counts: { fields_verified: '50', fields_rate_limited: '25', fields_judgment_unavailable: '25' },
  });
  assert.equal(c.proven, 50);
  assert.equal(c.fieldTotal, 100);
  assert.equal(c.counted, true);
});

/* ------------------------------------------------------------------ report */

console.log('');
if (failures) {
  console.log(`${failures} FAILURE${failures === 1 ? '' : 'S'}`);
  process.exit(1);
}
console.log('all view-model checks passed');