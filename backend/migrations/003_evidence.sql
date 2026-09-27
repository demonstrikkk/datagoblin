-- 003_evidence.sql — evidence substrate + honest run state.
--
-- Why this exists: the pipeline used to fetch a page, reduce it in RAM, extract
-- from it, then throw it away. Only a SHA-256 and a character count survived.
-- Evidence records stored `start`/`end` offsets into text that no longer
-- existed, so nothing could be re-verified after the run. A crawl -> RAM ->
-- extract -> discard pipeline cannot support the claim "every output traces
-- back to real evidence", so the page itself is now stored.
--
-- Idempotent: safe to re-run. Run via: python backend/scripts/db_migrate.py

-- runs: the run record could not tell the truth ---------------------------
-- It had no query, no error, no stats, and no `partial` flag, so a run that
-- died on the runtime budget was indistinguishable from a completed one
-- except by a free-text message. `partial` makes "we kept some of it" a
-- queryable fact instead of a string someone has to grep for.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS query text NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS partial boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS error text NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS stats jsonb NOT NULL DEFAULT '{}'::jsonb;

-- pages: the stored evidence ----------------------------------------------
-- One row per settled page. `markdown` is the reduced text the LLM actually
-- saw (that is what quotes and offsets must resolve against); `raw_html` is
-- the snapshot for re-checking `content_hash` and for anything the reducer
-- dropped. `parent_url` + `depth` make the crawl tree reconstructable.
CREATE TABLE IF NOT EXISTS pages (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  url text NOT NULL,
  final_url text NOT NULL DEFAULT '',
  parent_url text NOT NULL DEFAULT '',
  depth int NOT NULL DEFAULT 0,
  method text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'ok',
  error text NOT NULL DEFAULT '',
  content_hash text NOT NULL DEFAULT '',
  markdown text NOT NULL DEFAULT '',
  raw_html text NOT NULL DEFAULT '',
  snapshot_chars int NOT NULL DEFAULT 0,
  retrieved_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pages_run_id_idx ON pages (run_id);
-- One stored page per URL per run: re-fetching the same URL updates the
-- evidence rather than silently forking it into two contradicting snapshots.
CREATE UNIQUE INDEX IF NOT EXISTS pages_run_url_idx ON pages (run_id, url);

-- datasets: record provenance needs to name the page it came from.
-- The evidence block currently lives inside dataset_records.row_json, so the
-- page reference is threaded through that JSON rather than duplicated here.
-- This index supports the "which records cite this page" question that
-- re-verification will ask.
CREATE INDEX IF NOT EXISTS pages_content_hash_idx ON pages (content_hash);

-- RLS: the backend holds the credentials server-side, matching 001_core.sql.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['pages'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'svc_all_' || t, t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO anon, authenticated '
                   'USING (true) WITH CHECK (true)', 'svc_all_' || t, t);
    EXECUTE format('GRANT ALL ON %I TO anon, authenticated', t);
  END LOOP;
END $$;
