-- 001_core.sql — DATAGOBLIN core persistence (mirrors SupabaseRepo surface).
-- Idempotent: safe to re-run. RLS enabled with service-backend policies
-- (TO anon, authenticated, full access: the backend holds the keys server-side;
-- no per-user rows exist). Per skill security-rls-basics + schema-data-types:
-- timestamptz everywhere, text over varchar(n), FKs indexed.
-- Run via: python backend/scripts/db_migrate.py

-- workflows ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflows (
  id uuid PRIMARY KEY,
  prompt text NOT NULL DEFAULT '',
  plan_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- runs -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  id uuid PRIMARY KEY,
  workflow_id uuid REFERENCES workflows (id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'DISCOVERING',
  current_stage text NOT NULL DEFAULT 'DISCOVERING',
  progress int NOT NULL DEFAULT 0,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS runs_workflow_id_idx ON runs (workflow_id);

-- sources --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
  run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  url text NOT NULL,
  title text NOT NULL DEFAULT '',
  content_hash text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'ok',
  error text NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, url)
);
CREATE INDEX IF NOT EXISTS sources_run_id_idx ON sources (run_id);

-- run_events -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  stage text NOT NULL DEFAULT '',
  message text NOT NULL DEFAULT '',
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS run_events_run_id_idx ON run_events (run_id);

-- datasets -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasets (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  name text NOT NULL DEFAULT '',
  schema_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  record_count int NOT NULL DEFAULT 0,
  sources_attempted int NOT NULL DEFAULT 0,
  sources_successful int NOT NULL DEFAULT 0,
  sources_failed int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS datasets_run_id_idx ON datasets (run_id);

-- dataset_records --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dataset_records (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES datasets (id) ON DELETE CASCADE,
  row_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS dataset_records_dataset_id_idx ON dataset_records (dataset_id);

-- exports --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exports (
  id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES datasets (id) ON DELETE CASCADE,
  format text NOT NULL DEFAULT '',
  byte_size int NOT NULL DEFAULT 0
);

-- RLS + grants (service backend owns all rows; see header note) ---------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['workflows','runs','sources','run_events',
                           'datasets','dataset_records','exports'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'svc_all_' || t, t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO anon, authenticated '
                   'USING (true) WITH CHECK (true)', 'svc_all_' || t, t);
    EXECUTE format('GRANT ALL ON %I TO anon, authenticated', t);
  END LOOP;
END $$;
