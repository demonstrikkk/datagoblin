-- Source revisions: make a page id an immutable reference to specific text.
--
-- What this fixes
-- ---------------
-- `003_evidence.sql` created `UNIQUE (run_id, url)` with the stated intent:
--
--   "One stored page per URL per run: re-fetching the same URL updates the
--    evidence rather than silently forking it into two contradicting snapshots."
--
-- The constraint did what it said and the consequence was the opposite of safe.
-- `PostgresRepo.upsert_page` wrote `ON CONFLICT (run_id, url) DO UPDATE` and did
-- not put `id` in the SET list, so the row kept its primary key while
-- `markdown` was replaced. Evidence is cited by `page_id`, never by
-- (run_id, url), so a re-fetch of the same URL inside the same run left every
-- `start`/`end` already recorded against that id addressing different text.
--
-- Nothing detected it. `locate_quote` was not re-run, the offsets were not
-- recomputed, and a cell that had been verified against one rendering was
-- still labelled verified while its quote no longer appeared at the position it
-- claimed. The constraint prevented two rows; it could not prevent evidence
-- drift. That traded a detectable contradiction for a silent one.
--
-- The rule, stated once
-- ---------------------
-- A `page_id` that has been cited always addresses the same text, forever.
--
-- So `pages` becomes append-only per content change, and an identical re-fetch
-- stays idempotent rather than forking a duplicate:
--
--   same content_hash  -> return the existing id, write nothing
--   different content  -> new id, revision + 1
--
-- Identical is the common case -- BFS re-encounters, a retried rung, a
-- gap-fill pass that re-searches and lands on a URL the run already read -- and
-- forking those would inflate page counts, waste backfill's candidate budget and
-- double-count sources. Only a genuine content change earns a second row.
--
-- `reducer_version`
-- -----------------
-- `markdown` is not raw HTML. It is the output of
-- `reducer.page_evidence_text()`, and the character offsets in every verified
-- cell index into *that* function's output. Change the function and every offset
-- in the database silently reinterprets, with no error and no way to tell which
-- rows were affected.
--
-- So each page records which reducer produced its stored text. That alone is
-- write-only data, so `stale_reducer_pages()` goes with it: the point is that a
-- reducer change becomes *detectable* rather than merely suspected.
--
-- Additive and reversible. No column is dropped or retyped and no row is
-- deleted. Dropping the unique index cannot violate anything, since the
-- constraint guaranteed there was at most one row per (run, url) already.

ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS revision        int  NOT NULL DEFAULT 1;
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS reducer_version text NOT NULL DEFAULT '';

-- The uniqueness has to go: it is what made the id mutable. Replaced by an
-- index that supports the lookup that replaced it -- "the newest revision of
-- this URL in this run", which is what `upsert_page` and `find_page_by_url`
-- both ask.
DROP INDEX IF EXISTS pages_run_url_idx;
CREATE INDEX IF NOT EXISTS pages_run_url_idx ON pages (run_id, url, revision DESC);

COMMENT ON COLUMN pages.revision IS
  '1-based. A page re-fetched with different content gets a new row with a new '
  'id and the next revision; the old row is retained because evidence cites it. '
  'An identical re-fetch reuses the id and adds no row.';
COMMENT ON COLUMN pages.reducer_version IS
  'reducer.REDUCER_VERSION that produced `markdown`. Offsets in every verified '
  'cell index into this reducer''s output, so a change to the reducer makes '
  'historical offsets unreinterpretable; this column is what makes that '
  'detectable. See services/reducer.py.';