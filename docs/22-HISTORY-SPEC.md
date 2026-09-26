# 22 — HISTORY SPEC

Each history entry retains: original prompt, generated plan (plan_json), run status/stages/progress/timestamps/errors, sources (url/title/status), dataset (schema + records + counts). Chain: History → Run → Plan → Dataset → Evidence.

`GET /api/history` returns runs joined with workflows + datasets, newest first. Click → `/runs/:id` trace + `/datasets/:id` data + plan view + sources.
