# 14 — DATABASE SCHEMA (Supabase Postgres = app truth)

Dynamic datasets live in JSON/JSONB + separate schema doc. Never hardcode entity columns.

- `workflows(id uuid pk, user_id text, prompt text, plan_json jsonb, created_at timestamptz)`
- `workflow_plans` — optional normalized view of plan_json (may collapse into workflows.plan_json; keep one canonical)
- `runs(id uuid pk, workflow_id fk, status text[07 states], current_stage text, progress int, started_at, completed_at, error text)`
- `run_events(id uuid pk, run_id fk, stage text, message text, metadata_json jsonb, timestamp timestamptz)`
- `sources(id uuid pk, run_id fk, url text, title text, content_hash text, retrieved_at timestamptz, status text, error text)`
- `datasets(id uuid pk, run_id fk, name text, schema_json jsonb, record_count int, sources_attempted/successful/failed int, created_at)`
- `dataset_records(id uuid pk, dataset_id fk, row_json jsonb[provenance-wrapped fields], created_at)`
- `record_fields` — optional shredding of row_json for query speed (may omit; JSONB sufficient for MVP)
- `exports(id uuid pk, dataset_id fk, format text, url_or_path text, created_at)`

Qdrant (optional) stores only id→vector for L3; DuckDB (future) reads exports for analytics. Supabase free-tier note: capacity verified at deploy time, not hardcoded here.
