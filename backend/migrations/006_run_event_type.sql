-- 006_run_event_type.sql — events become queryable by what happened.
--
-- Why this exists: `run_events` stored a stage and a message, and the message
-- was the only place the event's *kind* appeared — "Extracted 3 from ...", as
-- free text. So "show me every page that failed", "show me every record that
-- was rejected", or "count the fetches this run made" each meant a LIKE over a
-- sentence, and each answer was wrong the moment a message was reworded. The
-- event kinds were already fixed strings in code (`stage.started`,
-- `source.fetched`, `record.rejected`, `duplicate.merged`, `run.partial`), so
-- the data to filter on existed and was simply not being stored.
--
-- Backfilled from the message for existing rows, conservatively: a row whose
-- message matches a known kind is given that kind, and a row that does not
-- match is left as `''` rather than guessed at. An event that cannot be
-- identified is honestly unidentified, and a wrong `type` is worse than an
-- absent one because a query on it will then return a confident wrong answer.
--
-- Idempotent: safe to re-run. Run via: python backend/scripts/db_migrate.py

ALTER TABLE run_events ADD COLUMN IF NOT EXISTS type text NOT NULL DEFAULT '';

COMMENT ON COLUMN run_events.type IS
  'event kind (stage.started, source.fetched, record.rejected, ...); empty when '
  'the kind could not be identified from an existing message';

-- Backfill, best-effort and non-destructive: a re-run re-derives from the
-- message, which never changes, so this is stable.
UPDATE run_events SET type = 'stage.started'  WHERE type = '' AND message LIKE 'Discovering%';
UPDATE run_events SET type = 'stage.started'  WHERE type = '' AND message LIKE 'Fetching %';
UPDATE run_events SET type = 'stage.started'  WHERE type = '' AND message LIKE 'Extracting (%';
UPDATE run_events SET type = 'stage.started'  WHERE type = '' AND message LIKE 'Validating%';
UPDATE run_events SET type = 'stage.started'  WHERE type = '' AND message LIKE 'Deduplicating%';
UPDATE run_events SET type = 'source.fetched'  WHERE type = '' AND message LIKE 'Extracted %';
UPDATE run_events SET type = 'source.reused'  WHERE type = '' AND message LIKE 'reused stored page%';
UPDATE run_events SET type = 'record.rejected' WHERE type = '' AND message LIKE 'rejected%';
UPDATE run_events SET type = 'duplicate.merged' WHERE type = '' AND message LIKE 'merged%';
UPDATE run_events SET type = 'run.completed'   WHERE type = '' AND message LIKE 'run complete%';

-- The whole point: filtering by kind instead of by sentence.
CREATE INDEX IF NOT EXISTS run_events_run_type_idx ON run_events (run_id, type);
