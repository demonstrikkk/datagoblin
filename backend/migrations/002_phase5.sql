-- 002_phase5.sql — metering ledger + URL fingerprint store.
-- Idempotent: safe to re-run. usage_ledger.run_id has NO foreign key by
-- design: map-* job ids are not runs, and ledger rows must survive run-row
-- cleanup. seen_fingerprints.run_id is likewise plain text (provenance, not
-- referential). RLS + service policies + grants mirror 001_core.sql.
-- Run via: python backend/scripts/db_migrate.py

-- usage_ledger -----------------------------------------------------------------
-- charge_id is the idempotency key (upsert target in SupabaseRepo).
CREATE TABLE IF NOT EXISTS usage_ledger (
  charge_id text PRIMARY KEY,
  run_id text NOT NULL DEFAULT '',
  stage text NOT NULL DEFAULT '',
  units int NOT NULL DEFAULT 0,
  credits int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_ledger_run_id_idx ON usage_ledger (run_id);

-- seen_fingerprints --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seen_fingerprints (
  fp text PRIMARY KEY,
  run_id text NOT NULL DEFAULT '',
  url text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS seen_fingerprints_run_id_idx ON seen_fingerprints (run_id);

-- RLS + grants ---------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['usage_ledger','seen_fingerprints'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'svc_all_' || t, t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO anon, authenticated '
                   'USING (true) WITH CHECK (true)', 'svc_all_' || t, t);
    EXECUTE format('GRANT ALL ON %I TO anon, authenticated', t);
  END LOOP;
END $$;
