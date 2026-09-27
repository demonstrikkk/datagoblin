# 16 — SSE EVENT CONTRACT

Endpoint: `GET /api/runs/{run_id}/stream` (SSE, not WebSockets).

Envelope:

```json
{"type":"stage.started","run_id":"...","stage":"EXTRACTING","message":"Extracting",
 "progress":55,"timestamp":"2026-09-27T16:20:11Z","data":{}}
```

## The exact emitted set

```
run.partial
stage.started   stage.progress
source.discovered   source.fetched
record.extracted   record.verified   record.needs_review   record.rejected
duplicate.merged
run.completed   run.failed   run.cancelled
```

This is enforced by a test that reads the emit sites and compares both
directions, so the list cannot drift again.

**Removed because nothing ever sent them:** `run.created`, `duplicate.detected`,
`stage.completed`. They were declared in code and in this document for a long
time and honoured by nothing. Run creation is conveyed by the
`POST /api/runs` response body instead.

**Added:** `run.partial` (a budget-stopped run that still stored records) and
`record.needs_review` (a record that was **kept** with unproven fields).

> `record.rejected` previously fired 129 times against 0 dropped rows, because
> validation never actually dropped anything and the event was reused for
> "needs a human". Rejection now means dropped, and the two are distinct.

## Terminal events

| event | run status | dataset |
|---|---|---|
| `run.completed` | `COMPLETED` | written |
| `run.partial` | `PARTIAL`, `partial=true` | written, if any record survived |
| `run.failed` | `FAILED` | none |
| `run.cancelled` | `CANCELLED` | none |

`run.partial` is emitted with the real count — including **zero**, with a
message saying nothing was kept. The old timeout path emitted "partial kept"
while discarding every record.

## Stages

`PLANNING → DISCOVERING → FETCHING → REDUCING → EXTRACTING → VALIDATING →
DEDUPLICATING → FINALIZING → COMPLETED`, with `FAILED` / `CANCELLED`
terminal.

`REDUCING` and `FINALIZING` are declared in the stage enum and legal-transition
table but are **never emitted** — reduction happens inside extraction and
finalization is the unlabelled store call. They are left in place rather than
removed because the transition table is part of the published contract.

There is deliberately **no `PARTIAL` stage**. A run can be `PARTIAL` in status
while its last stage is `FAILED`; inventing a stage for "stopped early" would
imply a pipeline step that does not exist.

## Known gap

`run_events` has **no `type` column and no `timestamp`** (see docs/14). The
persisted audit trail cannot distinguish `record.rejected` from
`run.completed` after the fact; the type lives only in the SSE payload. Closing
this needs a migration.
