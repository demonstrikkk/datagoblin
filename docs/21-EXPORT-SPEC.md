# 21 — EXPORT SPEC

Formats: CSV, JSON (required); Parquet optional (via DuckDB/pandas later).

Rules: exported columns = plan fields + `_source_url, _verification_status, _retrieved_at` per field-group (never silently drop provenance); unverified values export as empty + status column; include dataset meta header (prompt, plan goal, created_at, source counts). `POST /api/datasets/{id}/export` returns download; frontend ExportMenu offers CSV/JSON.
