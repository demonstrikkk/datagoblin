# 16 — SSE EVENT CONTRACT

Endpoint: `GET /api/runs/{run_id}/stream` (SSE, not WebSockets).

Event envelope:

```json
{"type":"stage.started","run_id":"...","stage":"extracting","message":"Extracting records","progress":62,"timestamp":"...","data":{}}
```

Types (exact set): `run.created, stage.started, stage.progress, source.discovered, source.fetched, record.extracted, record.verified, record.rejected, duplicate.detected, duplicate.merged, stage.completed, run.completed, run.failed, run.cancelled`.

Frontend: timeline checks off PLANNING→...→FINALIZING, counters (records x/y, fields, verified/needs-review), activity feed appends messages with timestamps. See contracts/run-event.schema.json.
