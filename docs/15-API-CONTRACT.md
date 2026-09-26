# 15 — API CONTRACT (exact; do not invent endpoints)

- `POST /api/workflows/compile` {prompt} → {plan_id, plan}
- `POST /api/runs` {plan_id} → {run_id, status}
- `GET /api/runs/{id}` → {run_id, status, current_stage, progress, counters, error}
- `GET /api/runs/{id}/stream` (SSE) → events per 16-SSE-EVENT-CONTRACT
- `POST /api/runs/{id}/cancel` → {run_id, status:CANCELLED, partial_dataset_id?}
- `GET /api/datasets` → list {id, name, record_count, created_at}
- `GET /api/datasets/{id}` → {id, name, schema, records_preview, sources_summary}
- `GET /api/datasets/{id}/records` → paginated rows (provenance-wrapped) + filters
- `GET /api/datasets/{id}/sources` → source list with health
- `POST /api/datasets/{id}/export` {format: csv|json} → {export_id, download_url}
- `GET /api/history` → runs joined with workflows+plans+datasets (see 22)

Error shape: `{error:{code, message, details?}}`. Validation failures → 422. Full schemas in contracts/api.openapi.yaml.
