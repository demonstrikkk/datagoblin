# 38 — API SHAPE FINDINGS

Discovered 2026-09-27 while building the frontend against a live backend
(commit under test: the `main.py` revision with `003_evidence.sql` applied).

These are **not** fixed. They are recorded because the frontend currently
compensates for each one, and a future backend fix must not silently break the
UI that is working around it. Every item states what the frontend does today so
the compensation can be removed deliberately rather than by accident.

---

## 1. The same run reports three different counter shapes

This is the most consequential inconsistency found.

| Source | Shape |
|---|---|
| Live in-memory run (`GET /runs/{id}`) | `counters` — 9 keys: `attempted`, `successful`, `failed`, `records`, `records_fully_verified`, `records_needing_review`, `fields_verified`, `fields_judgment_unavailable`, `fields_rate_limited` |
| Stored run after eviction (`GET /runs/{id}` falls back to `repo().get_run`) | `counters` — 6 **legacy** keys: `attempted`, `successful`, `failed`, `records`, `verified`, `needs_review` |
| `GET /history` / stored row | `stats.counts` — 15 keys, a **superset**: adds `fields_unverified`, `fields_conflicting`, `skipped`, `partial`, `partial_reason`, `no_yield_reason` |

**Consequence.** A finished run's quality breakdown disappears depending on
which endpoint you ask. `/runs/{id}` returns the flattened row and loses all
field-level detail, so `fields_verified` is `undefined` and the UI shows no
evidence-quality bar at all — even though `/history` has the full data for the
same run.

**Frontend today:** `runCounters(...candidates)` in `lib/format.js` accepts
several run objects and returns whichever actually carries field-level keys.
`RunView` passes both the polled run and the history row. It also always
returns `{}` rather than `null` — see finding 6.

**Fix when possible:** make `repo().get_run` return `stats.counts`, or have
`GET /runs/{id}` prefer the rich shape. The frontend comparison can then be
collapsed to a single source.

---

## 2. `GET /runs/{id}` drops `partial` and `no_yield_reason` for live runs

`_run_view()` in `main.py` returns both, and they are written to the `RUNS`
registry. But the endpoint builds its response from a `RunView` model that has
no such fields, so the final `model_dump()` omits them.

**Consequence.** A budget-stopped run reports `status: "PARTIAL"` but not
`partial: true`, and never reports why it stopped short. Clients that read the
flag alone misclassify it.

**Frontend today:** `isPartialRun()` accepts `partial === true`, `status ===
'PARTIAL'`, or `counters.partial`, and `RunView` reads the run's `error`
string, which does carry the reason.

---

## 3. `partial` runs never close their SSE stream

```python
if done or (RUNS.get(run_id, {}).get("status") in ("COMPLETED", "FAILED", "CANCELLED") and idx >= total):
    break
```

`PARTIAL` is not in that tuple, so the generator polls `bus.snapshot` every
0.5s indefinitely. The client's socket stays open and the UI appears to hang on
a run that has already finished.

**Frontend today:** `useRunStream` treats a terminal status received over the
stream as terminal and closes the socket itself. This is a client-side patch
for a server-side leak; the connection is not released on the server.

---

## 4. `records` and `sources` return enveloped objects, not arrays

```
GET /api/datasets/{id}/records  ->  { dataset_id, total, records: [...] }
GET /api/datasets/{id}/sources  ->  { dataset_id, sources_attempted,
                                       sources_successful, sources_failed,
                                       sources: [...] }
```

Both 404 when the *array* is empty, which conflates "dataset not found" with
"dataset has no records".

**Frontend today:** reads `data.records` / `data.sources` and uses
`data.total` for real pagination. The `sources_attempted/successful/failed`
counters are shown as a fetch-rate summary, which is the only place those
numbers are surfaced at all.

**Note:** this is a *good* shape — `total` is what makes server-side pagination
possible. Only the empty-array 404 is questionable.

---

## 5. Dataset `schema` and `counts` are inconsistently persisted

Some rows in `/api/datasets` return an empty `schema` and an all-zero `counts`
object (`{records: 0, verified: 0, needs_review: 0}`), while
`GET /api/datasets/{id}` for the same dataset returns a 15-field schema. The
list endpoint appears to read a different column than the detail endpoint
(`schema` vs `schema_json`).

**Consequence:** an all-zero `counts` does **not** mean zero verification. The
per-field `verification_status` is stored on every record regardless. A UI that
reads `counts` alone will confidently report "unproven" for a dataset whose
fields are largely verified — an unearned negative.

**Frontend today:** the Library distinguishes *counted* from *not counted* and
shows a quality bar only when counters exist, with a single explanatory notice
rather than the same caveat repeated on every card.

---

## 6. `GET /api/models/free` has no `reachable` field

`text_models[]` entries are `{id, transport, vendor, data, observed, note}`.
There is no `reachable` boolean. `observed` is a string such as
`"ok 2026-09-27"`, and `gated_note` explains that most free models answer
`403 FreeTierError` on direct Zen and are reachable only through the local
OpenCode server.

A client defaulting to `reachable !== false` paints all ten models green
including the ones known to be gated. `opencode` is
`{reachable, base_url, enabled, error, http_status}` — note `reachable`, not
`available`.

**Frontend today:** derives state from `observed` and `transport`; green only
when `observed` starts with `ok`, amber for `transport: "opencode"`.

---

## 7. The event loop blocks during a run

Observed directly: with a run in flight, `GET /api/health` — a trivial
`async def` — stopped responding for **over two minutes**, and `Invoke-RestMethod`
against `/api/history` hung until its 120s timeout. It recovered only after the
run's judging phase finished (Jev call count went 169 → 216 across the stall).

Suspected causes, in order of likelihood:

- **Blocking sleeps in the retry path.** Jev 429s back off exponentially. A
  `time.sleep` inside an `async def` blocks the whole loop for the backoff
  duration; `asyncio.sleep` would not.
- **Sync psycopg on the loop.** `_emit` calls `r.append_event()` directly.
  Note `_persist_page` *is* correctly offloaded via `asyncio.to_thread`, which
  shows the pattern is known — it is just not applied everywhere.
- **Blocking HTTP in the provider clients.** A sync `httpx`/`requests` call or a
  sync Zen retry inside the async provider path.

**Consequence.** The API is unavailable for minutes at a time during exactly the
period a user is watching the UI. Health polling flips to "unreachable", run
polling times out, and the app looks broken while working correctly.

**Frontend today:** degrades honestly rather than spinning — the health pulse
shows `offline` when `/api/health` fails, `useResource` surfaces a real timeout
message, and `RunView`'s poller swallows the failure and keeps retrying because
the SSE stream is still the source of truth for the event log.

**This is the highest-value backend fix on the list.** It is also the most
plausible explanation for the "the app hangs" reports that prompted the UI
rebuild in the first place.

---

## 8. Minor: run identity is spelled two ways

`GET /runs/{id}` and `POST /runs` return `run_id`. `GET /history` and the stored
row return `id`. Reading only one produced blank identifiers and a run list with
no working links.

**Frontend today:** `runId(run)` accepts either.

---

## 9. Minor: `CSV`/`MD` export bypasses the envelope

`POST /api/datasets/{id}/export` returns `PlainTextResponse` for `csv` and `md`,
but the normal `{data, error, meta}` envelope for `json`. A client that
uniformly requests a Blob receives `{"data":…}` bytes for JSON.

**Frontend today:** `api.exportDataset` branches on format and returns
`{blob, format}` for the attachment formats and parsed data for JSON.
