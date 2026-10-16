-- Source reputation and freshness.
--
-- Why this is a separate table and not columns on `sources`:
--
-- `sources` is per (run, url) — the same host appears once per run, and a host
-- that was authoritative eighteen months ago sits beside one fetched today.
-- Ranking refreshes by "which host has historically produced verified cells"
-- needs one row per host across all runs, not one per visit. Aggregating on read
-- meant a full scan of `sources` joined to `pages` on every refresh, and — the
-- real problem — there was nothing to aggregate. `sources` recorded that a fetch
-- happened. It recorded nothing about whether the page produced anything usable,
-- so a host could be blocked for a month and still rank as highly as one that
-- answered perfectly every time.
--
-- What is stored, and why each column earns its place:
--
--   visits, successes        — did it answer at all. A host that 404s or blocks
--                              is not a source, whatever its history.
--   verified_cells, cells    — the real signal: of everything asked of it, how
--                              much came back proven. This is what separates a
--                              site that answers with press releases from one
--                              that answers with the numbers asked for.
--   block_count              — throttles and 403s. Weighted separately because
--                              "slow" and "refusing" are different faults and
--                              averaging them together hides both.
--   first_seen/last_seen     — freshness. A high score earned two years ago is
--                              not the same claim as one earned last week.
--
-- `trust_score` is deliberately NOT stored as the single answer. It is derived
-- in `services/reputation.py` from these raw counts, because the decay rate is a
-- policy choice that will change, and a stored score cannot be recomputed when it
-- does without rewriting every row. The raw counts are the durable fact; the
-- score is a view over them.
--
-- Additive only. No column is dropped or retyped, so this is safe to apply to a
-- live database and safe to leave in place if it is later not wanted.

CREATE TABLE IF NOT EXISTS source_reputation (
    host            text PRIMARY KEY,
    visits          int     NOT NULL DEFAULT 0,
    successes       int     NOT NULL DEFAULT 0,
    cells           int     NOT NULL DEFAULT 0,
    verified_cells  int     NOT NULL DEFAULT 0,
    block_count     int     NOT NULL DEFAULT 0,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    last_blocked_at timestamptz
);

COMMENT ON TABLE source_reputation IS
  'Cross-run per-host record of how much usable, verified data each host '
  'produced. Derived scores are computed, not stored.';

-- The refresh path ranks hosts by score and takes a prefix, so it needs an
-- ordering it can use directly.
CREATE INDEX IF NOT EXISTS source_reputation_rank_idx
    ON source_reputation (verified_cells DESC, last_seen DESC);

-- Age is part of the score, so "last used successfully" is read directly.
CREATE INDEX IF NOT EXISTS source_reputation_last_success_idx
    ON source_reputation (last_success_at DESC NULLS LAST);

-- Per-host, per-field freshness: "when was this field last proven?".
--
-- This is the table that makes staleness answerable rather than guessable. A
-- dataset-level timestamp says when the run happened; it cannot say that
-- `revenue` was verified last month while `company_name` was verified last week,
-- and those two fields have completely different decay rates. Refresh
-- prioritises by this table, so a field nobody has re-checked in six months
-- comes first.
CREATE TABLE IF NOT EXISTS field_freshness (
    dataset_id       uuid NOT NULL REFERENCES datasets (id) ON DELETE CASCADE,
    field            text NOT NULL,
    last_verified_at timestamptz,
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    verified_cells   int NOT NULL DEFAULT 0,
    total_cells      int NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset_id, field)
);

CREATE INDEX IF NOT EXISTS field_freshness_stale_idx
    ON field_freshness (dataset_id, last_verified_at ASC NULLS FIRST);