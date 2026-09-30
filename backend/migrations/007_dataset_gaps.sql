-- 007_dataset_gaps.sql — gaps stop being recomputed and start being tracked.
--
-- Why this exists: what a dataset is missing was derived on every read. The
-- Coverage view called `coverage.build_backlog`, which re-read every record and
-- rebuilt the queue from scratch, so the answer was always "now" and never
-- "then". Nothing recorded that a field had already been tried, which phase had
-- been tried, what it cost, or that the sources had been shown not to carry it.
--
-- That made the same expensive work repeatable indefinitely. A backfill re-read
-- stored pages for a field, found nothing, and left no trace — so the next
-- proposal offered to do exactly the same work again, and a caller could not
-- tell "never attempted" from "attempted twice and the sources do not have it".
-- Those need different answers and, eventually, different UI: one is a queue, the
-- other is a finding.
--
-- The two-phase remedy needs somewhere to record which phase a gap reached:
--
--   phase 0  never attempted
--   phase 1  re-read the pages the run already stored (no new requests)
--   phase 2  targeted web search, fetch, then extract from the new pages
--
-- `phase` only ever advances from a completed attempt. A gap that was proposed
-- but never run stays at 0, and a phase-2 gap that never found a page never
-- reaches 2. That is the whole point of the column: the UI must not be able to
-- show "we searched the web" for a field we only re-read.
--
-- `state` is the other half:
--
--   open       outstanding work
--   resolved   nothing outstanding against the field
--   exhausted  every phase was actually attempted and the sources did not
--              answer. This is a result, not a failure, and it is terminal:
--              re-running is refused rather than repeated.
--   refused    cannot be attempted safely (no dedupe_keys to match a new
--              extraction to a record). Distinct from exhausted: nothing was
--              tried, so nothing was learned.
--
-- Counts are a snapshot of the moment the gap was written, not a live join.
-- That is deliberate — a gap row is a record of an attempt, and recomputing it
-- on read would put back exactly the ambiguity this table exists to remove.
-- The live numbers stay available from `/coverage`, which is where "what is
-- missing right now" is answered; this table answers "what have we tried".
--
-- Idempotent: safe to re-run. Run via: python backend/scripts/db_migrate.py

CREATE TABLE IF NOT EXISTS dataset_gaps (
  id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES datasets (id) ON DELETE CASCADE,
  field text NOT NULL,

  -- Which kind of gap this is, and therefore which remedy could close it.
  --   schema_gap   no record carries the field at all
  --   depth_gap    some records carry it, some do not
  --   evidence_gap a value exists but carries no verdict, or sources disagree
  category text NOT NULL DEFAULT 'depth_gap',

  -- Outstanding cells at the moment of writing. A snapshot, as above.
  missing int NOT NULL DEFAULT 0,
  unverified int NOT NULL DEFAULT 0,
  conflicting int NOT NULL DEFAULT 0,

  state text NOT NULL DEFAULT 'open',
  phase int NOT NULL DEFAULT 0,
  attempts int NOT NULL DEFAULT 0,

  -- Why it stopped, in a sentence fit to show a person. Empty while open.
  reason text NOT NULL DEFAULT '',

  -- What the attempt actually did, so a repeat is not a mystery:
  --   pages_read, cells_written, searches_run, pages_fetched
  stats jsonb NOT NULL DEFAULT '{}'::jsonb,

  last_error text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);

-- One row per (dataset, field). This is the idempotency guarantee: re-running a
-- gap pass updates the same row rather than accumulating one row per pass, so
-- "how many times have we tried this" is a single number instead of a COUNT that
-- depends on how often somebody pressed the button.
CREATE UNIQUE INDEX IF NOT EXISTS dataset_gaps_dataset_field_idx
  ON dataset_gaps (dataset_id, field);

-- The scheduler's only query: what is worth attempting next. Partial, because a
-- resolved or exhausted gap is never a candidate and should not be in the index
-- at all.
CREATE INDEX IF NOT EXISTS dataset_gaps_open_idx
  ON dataset_gaps (dataset_id, phase, category)
  WHERE state = 'open';

COMMENT ON COLUMN dataset_gaps.phase IS
  'furthest phase actually attempted: 0 none, 1 stored pages re-read, 2 web search + fetch';
COMMENT ON COLUMN dataset_gaps.state IS
  'open | resolved | exhausted (tried everything, sources do not answer) | refused (unsafe to try)';
COMMENT ON TABLE dataset_gaps IS
  'persistent record of what was tried per missing field; the live "what is missing now" answer stays in /coverage';

-- RLS: the backend holds the credentials server-side, matching 001_core.sql.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['dataset_gaps'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'svc_all_' || t, t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO anon, authenticated '
                   'USING (true) WITH CHECK (true)', 'svc_all_' || t, t);
    EXECUTE format('GRANT ALL ON %I TO anon, authenticated', t);
  END LOOP;
END $$;
