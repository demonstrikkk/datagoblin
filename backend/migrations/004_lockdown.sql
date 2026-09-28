-- 004_lockdown.sql — remove the world-readable grant.
--
-- WHY THIS EXISTS
-- 001_core.sql enabled RLS and then granted `USING (true) WITH CHECK (true)`
-- to `anon, authenticated` with `GRANT ALL` on every table. That is
-- functionally identical to having no RLS at all: the policy matches every
-- row and permits every write. The justification in the 001 header was that
-- "the backend holds the keys server-side", which is true for THIS process but
-- is not a database control. Anyone holding the Supabase project URL and the
-- public anon key — which is a designed-to-be-public credential, shipped in
-- every Supabase client — could read and overwrite every run, dataset, record
-- and evidence quote, and could delete the corpus.
--
-- The backend connects as the `postgres` role, which owns these tables and
-- therefore bypasses RLS as the owner. Locking the policies down is
-- consequently safe for the app and closes the exposure.
--
-- This is deliberately ADDITIVE. 001 is not edited, because it has already
-- been applied and rewriting it would leave existing databases and fresh ones
-- on different schemas.
--
-- TO REVERT (local development only, never on a shared database):
--   DO $$ DECLARE t text; BEGIN
--     FOREACH t IN ARRAY ARRAY['workflows','runs','sources','run_events',
--                              'datasets','dataset_records','exports','pages'] LOOP
--       EXECUTE format('DROP POLICY IF EXISTS %I ON %I','svc_all_' || t, t);
--       EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO anon, authenticated '
--                      'USING (true) WITH CHECK (true)','svc_all_' || t, t);
--       EXECUTE format('GRANT ALL ON %I TO anon, authenticated', t);
--     END LOOP;
--   END $$;

DO $$
DECLARE
    t   text;
    seq text;
BEGIN
    -- Drop the permissive policies created by 001. RLS itself stays ENABLED:
    -- with no policy at all, a non-owner role sees zero rows, which is the
    -- correct default for a table only the backend should touch.
    FOREACH t IN ARRAY ARRAY['workflows','runs','sources','run_events',
                             'datasets','dataset_records','exports','pages'] LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
            -- RLS stays ENABLED but NOT FORCED. Forcing it would make the
            -- table owner subject to the very policies this migration drops,
            -- and with no policy left the backend (which connects as `postgres`,
            -- the owner) would read zero rows. The owner bypass is exactly
            -- what keeps the app working; the exposure being closed is the
            -- `anon` grant, not the owner.
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'svc_all_' || t, t);
            -- Revoke from the public-facing roles. OWNER keeps its implicit
            -- rights, which is how the backend continues to work unchanged.
            EXECUTE format('REVOKE ALL ON %I FROM anon', t);
            EXECUTE format('REVOKE ALL ON %I FROM authenticated', t);
            EXECUTE format('REVOKE ALL ON %I FROM public', t);
        END IF;
    END LOOP;

    -- Sequences carry no RLS; an unprivileged role that could still call
    -- nextval() could enumerate ids and infer row counts.
    FOR seq IN
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'S'
    LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE %I FROM anon', seq);
        EXECUTE format('REVOKE ALL ON SEQUENCE %I FROM authenticated', seq);
        EXECUTE format('REVOKE ALL ON SEQUENCE %I FROM public', seq);
    END LOOP;
END $$;

-- Fail loudly if this migration is ever applied by a role that is NOT the
-- table owner. A non-owner would be subject to the very policies being
-- dropped here, and would silently read nothing.
DO $$
DECLARE
    owner_name text;
    current_name text;
BEGIN
    SELECT tableowner INTO owner_name
      FROM pg_tables WHERE schemaname = 'public' AND tablename = 'runs';
    current_name := current_user;
    IF owner_name IS NOT NULL AND current_name <> owner_name THEN
        RAISE EXCEPTION
            '004_lockdown: applied by % but the tables are owned by %. '
            'A non-owner would be subject to the policies this migration drops. '
            'Apply migrations as the table owner.', current_name, owner_name;
    END IF;
END $$;
