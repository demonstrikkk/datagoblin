-- Record what happened to every fetch attempt, not just whether it returned bytes.
--
-- What this fixes
-- ---------------
-- A fetch returned "some HTML", and the run recorded that as success. There was
-- no field anywhere for whether a renderer had been opened, whether it had
-- worked, why it had not, or whether the text being extracted came from the
-- thing that was asked for.
--
-- The consequence was measured, not theoretical. On `aws.amazon.com/transform/`
-- a Chromium render produced 17635 characters of content; the render then died,
-- the waterfall fell back to the HTTP version, and the dataset stored 7885
-- characters of unrendered HTML -- 55% lost, with `pages.method` reading
-- `"http"`, no error, no event, and no counter. The failure was not unrecorded.
-- It was *erased*: `crawler._one` held the render exception in `last_err` and
-- returned the HTTP page without it.
--
-- So a stored page could not be told apart from a stored render, and a dataset
-- could not be told apart from one that had quietly lost its rendering.
--
-- On `pages` -- the evidence record
-- ---------------------------------
--   fetch_attempts        every rung tried, in order, with its outcome. The
--                         waterfall is not otherwise recoverable after the fact.
--   render_attempted      was a browser opened at all. Distinguishes "the page
--                         did not need one" from "we tried and failed".
--   render_status         not_needed | succeeded | failed | timed_out
--   render_error_code     one of `fetcher.RENDER_ERROR_CODES`. Free text in an
--                         error column cannot be counted, and "why do driver
--                         deaths happen" is unanswerable from a 150-char string.
--   fallback_used         true when the stored text is not what the rung that
--                         produced it would have produced.
--   final_content_source  rendered | http | none
--   degraded_reason       the error text, so the code is not the only evidence.
--
-- On `sources` -- the attempt record, which exists for failures too
-- -----------------------------------------------------------------
-- `pages` is only written when a fetch succeeds, so a failed or skipped source
-- has no page row and nowhere to record why. `render_status` and
-- `render_error_code` go here too, which is what makes "3 of 21 renders died"
-- answerable from the database rather than from a bench script.
--
-- Additive. No column is dropped or retyped, and every new column has a default,
-- so this is safe to apply to a live database and safe to leave in place.

ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS fetch_attempts      jsonb       NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS render_attempted    boolean     NOT NULL DEFAULT false;
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS render_status       text        NOT NULL DEFAULT 'not_needed';
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS render_error_code   text        NOT NULL DEFAULT '';
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS fallback_used       boolean     NOT NULL DEFAULT false;
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS final_content_source text       NOT NULL DEFAULT 'http';
ALTER TABLE pages
    ADD COLUMN IF NOT EXISTS degraded_reason     text        NOT NULL DEFAULT '';

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS render_status       text        NOT NULL DEFAULT 'not_needed';
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS render_error_code   text        NOT NULL DEFAULT '';
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS fallback_used       boolean     NOT NULL DEFAULT false;

-- The question "how often does the renderer die, and of what" is asked per run
-- and over all history. Both columns are low-cardinality, and grouping by them is
-- cheap; `fetch_attempts` is deliberately not indexed because it is only ever
-- read for a single page id.
CREATE INDEX IF NOT EXISTS pages_render_status_idx
    ON pages (run_id, render_status);
CREATE INDEX IF NOT EXISTS sources_render_error_idx
    ON sources (render_error_code) WHERE render_error_code <> '';

COMMENT ON COLUMN pages.render_status IS
  'not_needed | succeeded | failed | timed_out. "not_needed" means no browser was '
  'opened; it is distinct from "succeeded" because a page that never needed a '
  'renderer proves nothing about whether the renderer works.';
COMMENT ON COLUMN pages.fallback_used IS
  'True when the stored text came from a different rung than the one that was '
  'asked for -- typically a render that failed and was served over HTTP. The '
  'page is still real and still worth keeping; it is not what was requested.';
COMMENT ON COLUMN pages.final_content_source IS
  'rendered | http | none. The provenance of `markdown`, which every verified '
  'quote resolves against.';