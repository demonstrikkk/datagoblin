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
source.discovered   source.fetched   source.reused   source.failed
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

**Added:** `run.partial` (a budget-stopped run that still stored records),
`record.needs_review` (a record that was **kept** with unproven fields), and
`source.failed`.

> `source.failed` covers three outcomes that had no representation at all, which
> is why it exists rather than why it is useful. Failed and skipped fetches
> emitted nothing, so a run's history said a page had been fetched whenever one
> had been fetched and was silent about every one that had not — a client could
> not list what was tried and refused. The third case is the one that mattered:
>
> | `data.status` | meaning | stored? |
> |---|---|---|
> | `failed` | the fetch produced nothing usable | no `pages` row |
> | `skipped` | robots, depth, budget or a refused host | no `pages` row |
> | `degraded` | a render failed and the page was served over HTTP | **yes** |
>
> `degraded` is not a failure: the page is real, is kept, and is worth keeping.
> But it is not what was asked for. Measured, a render that produced 17635
> characters of content and then died was stored as 7885 characters of
> unrendered HTML with no event, no counter and no field to say so.
>
> `data.error_code` is one of `fetcher.RENDER_ERROR_CODES`
> (`browser_disconnected`, `browser_crash`, `navigation_timeout`,
> `browser_launch_failed`, `content_extraction_error`, `unknown`), so a client
> groups by cause instead of parsing a sentence. `unknown` is a real member of
> the set rather than an absence: a failure that cannot be classified is a
> finding, because it means the taxonomy does not describe what the browser is
> doing.

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
