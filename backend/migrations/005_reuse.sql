-- Reuse: when a run recognises a URL it already fetched, it points at the
-- stored page instead of fetching again.
--
-- Why the columns instead of a note in `error`: reuse is a fact about a
-- source, not a failure. Writing "reused" into `error` would make the Sources
-- view read as a run full of problems, and it would be indistinguishable
-- from a genuine fetch error to anything that counts failures.
--
-- Why it must be visible at all: a URL fingerprint cannot tell whether the
-- page changed upstream, so reused evidence may be stale. The only honest
-- handling is to record where it came from and when, and let the reader judge
-- the age of what they are looking at.
ALTER TABLE sources
  ADD COLUMN IF NOT EXISTS reused_from_run_id uuid REFERENCES runs (id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS reused_page_id uuid REFERENCES pages (id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS retrieved_at timestamptz;

COMMENT ON COLUMN sources.reused_from_run_id IS
  'run that originally fetched this URL; NULL when this run fetched it';
COMMENT ON COLUMN sources.reused_page_id IS
  'stored page reused as the evidence, when status = reused';
COMMENT ON COLUMN sources.retrieved_at IS
  'when the underlying page was actually fetched, reused or not';
