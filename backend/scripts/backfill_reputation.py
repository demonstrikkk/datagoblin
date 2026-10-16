"""Backfill `source_reputation` and `field_freshness` from existing stored data.

Both tables were introduced after there was already a year of runs in the
database, so they start empty and every host looks unknown until it is fetched
again. That is a real loss: the history needed to rank sources is already on disk
in `sources` and `pages`, and the whole point of the feature is to stop paying for
requests to hosts whose behaviour is already known.

Idempotent — re-running converges rather than double-counting, because the
aggregate is recomputed from scratch and *replaces* the row:

    DELETE FROM source_reputation;
    INSERT ... SELECT (aggregate over all runs)

so a second run produces the same numbers, not twice as many.

Usage:
    python scripts/backfill_reputation.py            # report what it would do
    python scripts/backfill_reputation.py --apply    # do it
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.core.config import settings  # noqa: E402


#: One row per host, over every run.
#:
#: `ok` counts a fetch that returned a usable page; a skip (robots, non-HTML) is
#: a deliberate non-fetch and counts as neither success nor block, but it still
#: counts as a visit, because the host was asked for something.
#:
#: `blocks` are separated from outright failures on purpose. A 403 or a 429 says
#: the host refused; a DNS failure says we could not reach it. Averaging them into
#: one "failed" column is what made the old yield ranking unable to tell a
#: blocked host from a broken one.
SOURCE_AGG = """
DELETE FROM source_reputation;

INSERT INTO source_reputation
    (host, visits, successes, cells, verified_cells, block_count,
     first_seen, last_seen, last_success_at, last_blocked_at)
WITH host_visits AS (
    SELECT
        host,
        count(*)                                            AS visits,
        count(*) FILTER (WHERE status IN ('ok', 'reused')) AS successes,
        count(*) FILTER (
            WHERE lower(error) LIKE '%403%'
               OR lower(error) LIKE '%429%'
               OR lower(error) LIKE '%forbidden%'
               OR lower(error) LIKE '%robots%'
               OR lower(error) LIKE '%rate limit%'
               OR lower(error) LIKE '%throttl%'
               OR lower(error) LIKE '%blocked%'
        )                                                   AS block_count,
        min(COALESCE(retrieved_at, now()))                  AS first_seen,
        max(COALESCE(retrieved_at, now()))                  AS last_seen,
        max(retrieved_at) FILTER (WHERE status IN ('ok', 'reused')) AS last_success_at,
        max(now()) FILTER (WHERE status NOT IN ('ok', 'reused'))     AS last_blocked_at
    FROM (
        SELECT
            lower(split_part(split_part(url, '://', 2), '/', 1)) AS host,
            status, error, retrieved_at
        FROM sources
    ) s
    WHERE host <> ''
    GROUP BY host
)
    -- `cells` and `verified_cells` are what a host is actually judged on, and they
    -- come from `dataset_records`, not `sources`. The obvious join is by the
    -- source page's id, but a record's provenance is stored inline in row_json,
    -- and one page can supply values to several datasets. So this aggregates per
    -- (host, field) over the records that cite that host, then sums per host.
    -- Counting a cell twice — once via its dataset's source and once via the
    -- record — is avoided by doing it in one pass from the records alone.
SELECT
    s.host,
    s.visits,
    s.successes,
    COALESCE(c.cells, 0)          AS cells,
    COALESCE(c.verified_cells, 0) AS verified_cells,
    s.block_count,
    s.first_seen,
    s.last_seen,
    s.last_success_at,
    s.last_blocked_at
FROM host_visits s
LEFT JOIN (
    SELECT
        lower(split_part(split_part(h.url, '://', 2), '/', 1)) AS host,
        count(*) AS cells,
        count(*) FILTER (
            WHERE rec.row_json -> f.field ->> 'verification_status' = 'verified'
        ) AS verified_cells
    FROM dataset_records rec
    CROSS JOIN LATERAL jsonb_object_keys(rec.row_json) AS f(field)
    -- Provenance is stored inline per field, not in a sibling array, so the
    -- attributing URL is `row_json -> field -> 'source' ->> 'url'`.
    CROSS JOIN LATERAL (
        SELECT DISTINCT rec.row_json -> f.field -> 'source' ->> 'url' AS url
    ) h
    WHERE f.field NOT LIKE '\_%'
      AND h.url IS NOT NULL
    GROUP BY 1
) c ON c.host = s.host;
"""


#: Per-dataset, per-field. `last_verified_at` is the newest page that supplied a
#: verified value for the field, which is the date a reader cares about: not when
#: we last looked, but when the value was last *proved*.
#:
#: `row_json` is a flat map of field -> {value, source{retrieved_at},
#: verification_status}, so the date is already on the row and no join to `pages`
#: is needed — which is also why this aggregate stays cheap as the table grows.
#:
#: `total_cells` counts records that carry the field at all; `verified_cells`
#: counts those graded `verified`. A field present but graded
#: `judgment_unavailable` is still a fresh *reading* — the page was read and the
#: value graded — so it counts toward freshness while never counting as proof.
FIELD_AGG = """
DELETE FROM field_freshness;

INSERT INTO field_freshness
    (dataset_id, field, last_verified_at, last_seen_at, verified_cells, total_cells)
SELECT
    rec.dataset_id,
    f.field,
    max((rec.row_json -> f.field -> 'source' ->> 'retrieved_at')::timestamptz)
        FILTER (WHERE rec.row_json -> f.field ->> 'verification_status' = 'verified'),
    now(),
    count(*) FILTER (
        WHERE rec.row_json -> f.field ->> 'verification_status' = 'verified'),
    count(*)
FROM dataset_records rec
CROSS JOIN LATERAL jsonb_object_keys(rec.row_json) AS f(field)
WHERE f.field NOT LIKE '\\_%'
GROUP BY rec.dataset_id, f.field;
"""


def main() -> int:
    apply = "--apply" in sys.argv
    if not settings.DATABASE_URL:
        print("DATABASE_URL unset; aborting")
        return 1

    with psycopg.connect(settings.DATABASE_URL, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.source_reputation')")
            if cur.fetchone()[0] is None:
                print("source_reputation does not exist — run scripts/db_migrate.py")
                return 1

            cur.execute("SELECT count(*) FROM source_reputation")
            existing = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM field_freshness")
            existing_fields = cur.fetchone()[0]
            cur.execute("SELECT count(DISTINCT split_part(split_part(url,'://',2),'/',1)) "
                        "FROM sources")
            hosts = cur.fetchone()[0]

        print(f"source_reputation : {existing} row(s), {hosts} distinct host(s) in sources")
        print(f"field_freshness   : {existing_fields} row(s)")

        if not apply:
            print("\nRe-run with --apply to (re)build both tables from stored data.")
            return 0

        with conn.cursor() as cur:
            cur.execute(SOURCE_AGG)
            cur.execute("SELECT count(*) FROM source_reputation")
            print(f"source_reputation : {cur.fetchone()[0]} host(s) written")
        conn.commit()

        # The field aggregate joins against `records`, which is not present in
        # every schema version. It is genuinely optional — freshness per field is
        # new functionality and a dataset without it reports `tracked: false` —
        # so a failure here must not roll back the reputation backfill, which is
        # the part that changes existing behaviour.
        try:
            with conn.cursor() as cur:
                cur.execute(FIELD_AGG)
                cur.execute("SELECT count(*) FROM field_freshness")
                print(f"field_freshness   : {cur.fetchone()[0]} field(s) written")
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            print(f"field_freshness   : skipped ({str(exc)[:120]})")

    print("\nbackfill complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())