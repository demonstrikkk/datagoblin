# 14 — DATABASE SCHEMA (Postgres = the app's truth)

Dynamic datasets live in JSON/JSONB. Never hardcode entity columns.

Columns below are verified against the live database, not aspirational. Apply
with `python backend/scripts/db_migrate.py` (migrations are idempotent).

## Tables

**`workflows`** — a compiled plan, so a `plan_id` survives a restart.
```
id uuid pk, prompt text, plan_json jsonb
```

**`runs`** — run state. `partial` and `error` exist because a run that hit its
budget was previously indistinguishable from one that finished.
```
id uuid pk, workflow_id uuid fk, status text, current_stage text, progress int,
started_at timestamptz, completed_at timestamptz,
query text, partial boolean, error text, stats jsonb
```
`status` is one of `DISCOVERING | COMPLETED | FAILED | CANCELLED`. A
budget-stopped run is **`FAILED` with `partial = true`** — never `COMPLETED`,
even though a dataset was written.

**`sources`** — per-URL metadata only. No body.
```
run_id uuid fk, url text, title text, content_hash text, status text, error text
PK (run_id, url)
```

**`pages`** — the stored evidence (see docs/29). This table is the reason a
record can be re-verified.
```
id uuid pk, run_id uuid fk, url text, final_url text, parent_url text, depth int,
method text, status text, error text, content_hash text,
markdown text, raw_html text, snapshot_chars int, retrieved_at timestamptz
UNIQUE (run_id, url)   -- re-fetching a URL updates evidence, never forks it
```

**`run_events`** — the audit trail.
```
id bigint pk, run_id uuid fk, stage text, message text, metadata_json jsonb
```
> **Known gap:** there is **no `type` column** and no `timestamp`. The event
> type lives only in the SSE payload, so the persisted trail cannot distinguish
> `record.rejected` from `run.completed` after the fact. See docs/16.

**`datasets`** / **`dataset_records`** — the output.
```
datasets: id uuid pk, run_id uuid fk, name, schema_json jsonb, record_count int,
          sources_attempted/successful/failed int, created_at
dataset_records: id bigint pk, dataset_id uuid fk, row_json jsonb
```
`row_json` holds the provenance-wrapped fields, including `source.page_id`.

**`usage_ledger`** — credit metering, idempotent on `charge_id`.
**`seen_fingerprints`** — URL dedup, PK `fp`.
**`exports`** — `id, dataset_id, format, byte_size`.

## Not present, and deliberately so

- **No `user_id` / multi-tenancy columns.** Single-user local product. RLS is
  enabled with a permissive service policy because the backend holds the keys.
- **No vector table.** `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION`
  exist in config and are **read by nothing** — there is no embedding provider
  wired, and `FEATURE_SEMANTIC_DEDUP=true` raises on purpose rather than
  half-working.
- **No `workflow_plans` / `record_fields`.** `plan_json` and `row_json` are
  sufficient at this scale.

## Persistence adapter

`PERSISTENCE` is explicit and reported by `GET /api/health`:

- `postgres` (default when `DATABASE_URL` is set) — this file.
- `local` — JSONL under `.datagoblin-local/`, for running with no
  infrastructure. Explicit opt-in; it is never selected silently.

The adapter used to be inferred from whether Supabase keys were present, and
their absence quietly dropped the app to JSONL while this fully-migrated,
empty database sat unused with nothing reporting it.
