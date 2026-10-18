# DATAGOBLIN — operator runbook

Everything needed to run, diagnose and extend this system. Written against the
code, not against an earlier plan; where the two disagreed, the code won and the
disagreement was recorded rather than smoothed over.

---

## 1. What this is

A pipeline that turns a natural-language request into a **dataset where every
stored value carries its own proof**: the verbatim text it came from, the page it
came from, and a verdict on whether that text actually supports it.

```
prompt → plan → discover → fetch → reduce → extract → validate → dedupe → store
                          ↑                    ↑           ↑
                          └──── reputation ────┘           └─ evidence + staleness
```

The design commitment is that a value without a verifiable quote is not stored as
verified. That constraint shapes every module: the ladder, the validator, the
provenance schema and the UI all exist to make "is this actually supported?"
answerable per cell rather than per dataset.

---

## 2. Running it

Three processes. `start-dev.ps1` opens all three, but each is independently
runnable and that is what the rest of this document assumes.

```powershell
# 1. The local reader (the primary model, on the free tier)
$env:OPENCODE_SERVER_PASSWORD = "<same value as .env>"
opencode serve --port 4096 --hostname 127.0.0.1

# 2. The API
$env:PYTHONPATH = "$PWD\backend"
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. The studio
cd frontend; npm run dev -- --port 5173 --strictPort
```

### Check it is actually runnable

```powershell
backend\.venv\Scripts\python.exe backend\scripts\preflight.py
```

This is the first thing to run when something looks wrong. It probes every
dependency for real rather than reading `.env`, and exits non-zero when a run
would fail:

```
[BLOCK ] reader (opencode)   the server at 127.0.0.1:4096 did not answer
[  ok  ] search (tavily)     Tavily answered (1 result)
[  ok  ] store               postgres reachable (14 datasets)
[  ok  ] driver pool        present
verdict: BLOCKED
```

**"Not runnable" means a run would search, crawl, extract nothing, and report
success.** With `OPENCODE_STRICT=true` there is no cloud fallback, so an
unreachable local reader is fatal by design — see §4.

---

## 3. Why a run can legitimately produce nothing

Zero records is not always a bug. The pipeline is built so that "I found nothing
I can prove" is a valid, distinguishable outcome rather than a failure:

| Reason | Where it shows up | What to do |
|---|---|---|
| Reader unreachable | `run.failed`, `preflight: true` | start `opencode serve` |
| No sources discovered | `no_yield_reason` | check Tavily, then `preflight.py` |
| Every source blocked | sources all `failed` with 403/429 | reputation shows the pattern |
| Pages fetched, nothing extracted | `no_yield_reason` names it | see §5, thin-page detection |
| Values extracted, none verified | `fields_verified: 0` | a real result: the quotes did not support the claims |

The last row is the one most worth internalising. A dataset of zero verified
values after a successful crawl is **correct behaviour**, not a regression.

---

## 4. Strict mode, and why it fails closed

`OPENCODE_STRICT=true` makes the local reader the *only* model. There is no
Groq/Gemini cascade (`generate.structured_generate` returns `opencode_structured`
directly when strict is on).

So an unusable reader cannot be tolerated, and `opencode.preflight()` refuses the
run in under a second — before a single Tavily query is spent:

| State | Strict | Loose |
|---|---|---|
| reachable | allowed | allowed |
| unreachable / 401 | **refused** | refused (it's a fault, not a choice) |
| `OPENCODE_ENABLED=false` | **refused** | allowed — a cloud fallback exists |
| `OPENCODE_BASE_URL` unset | **refused** | allowed — a cloud fallback exists |

The distinction in the last two rows is deliberate: switching a provider *off* is
a choice and there is somewhere else to go, whereas a server that is *broken*
has to be reported either way, because a "fallback" that exists only on paper
silently starts billing a different provider.

---

## 5. How a value gets its evidence — the ladder

Extraction tries deterministic routes before spending a model call. The walk
stops as soon as nothing is missing, so a page the cheap rungs can answer costs
nothing at all.

| Rung | Reads | Trust prior |
|---|---|---|
| `json` | a JSON payload, parsed exactly | `api` — 0.99 |
| `json_ld` | schema.org the page publishes | 0.95 |
| `labelled` | `Label: value` lines | 0.95 |
| `selectors` | a checked-in CSS/regex schema | `dom` — 0.92 |
| `llm` | everything still missing | lowest |

Each rung returns a **flat** `{value, quote, read_method}`. Nesting the cell dict
inside `value` stores a dict as the value and the record is silently discarded —
this is the single most common way to break a rung, and `test_json_rung.py`
pins the shape.

The quote must be a substring of the text the *store* persists
(`reducer.page_evidence_text`). Both sides call the same function so they cannot
drift. A rung that quotes correctly but whose quote is absent from the stored
text loses everything at verification.

### The JSON rung

A surprising share of useful "pages" are endpoints. `source_router` recognises
them (`/api` in the path, or a `.json` extension) and `fetcher.http_fetch` now
returns them as `json_text` rather than discarding them as `non-html`.

It is conservative on purpose: a field is filled only when a key matches by
normalised name, alias, or whole-token overlap. `revenue_forecast` does **not**
answer `revenue` — that is a projection, and a confident wrong answer is worse
than a missing value, because the model rung is still asked.

---

## 6. Source reputation and staleness

Two questions nothing else answered, which is why it felt unreliable over time.

### Is this host worth a request?

`source_reputation` keeps raw counters per host, across every run. The score is
**derived, not stored** — `services/reputation.trust` computes it from
`verified_cells / cells` (decayed), `successes / visits`, and a capped block
penalty. Stored scores cannot be recomputed when the decay policy changes.

Against real data from this database:

```
job-boards.greenhouse.io   0.78   3 cells,    3 verified
in.indeed.com              0.68   565 cells,  310 verified
dealroom.co                0.55   1209 cells, 432 verified
www.linkedin.com           0.00   8 visits,   8 blocks
www.justdial.com           0.00   3 visits,   3 blocks
```

The ranking now separates four things that the old per-dataset yield ranking
could not: excellent-and-current, excellent-two-years-ago, answers-but-proves-
nothing, and refuses-to-answer.

`refresh.pick_sources` uses it, so refresh budget goes to the hosts that have
historically produced verified cells *and* recently did so.

### How old is this?

`field_freshness` records, per dataset per field, when a value was last
**verified**. Thresholds differ by volatility — `annual_revenue` goes stale at
14 days, `company_name` at 365. One global number would either present last
year's figures as current or re-fetch stable names forever.

```powershell
# what is stale, and in what order to re-check it
curl http://127.0.0.1:8000/api/datasets/<id>/staleness
```

```
fields=4 stale=1 (25.0%) oldest=13.1d
refresh order: founders, latest_funding, active_engineering_roles, startup_name
```

### Rebuilding the tables

Both were added after a year of runs already existed, so they start empty:

```powershell
backend\.venv\Scripts\python.exe backend\scripts\backfill_reputation.py --apply
```

Idempotent — it recomputes and replaces, so re-running converges rather than
double-counting.

---

## 7. Budgets and metering

| Control | Where | Meaning |
|---|---|---|
| `JudgeBudget(limit)` | `validator.py` | **`limit=0` means ZERO calls.** `unlimited=True` is the opt-in. |
| `Meter(job_id, budget)` | `metering.py` | Per-run credit budget. A `Meter` gets a private `Ledger`. |
| `SHARED_LEDGER` | `metering.py` | Shared by `/map`, `/export`, `/intel` so `charge_id` dedup spans requests. |
| `RUN_MAX_*` | `.env` | Stage caps. Env cannot exceed the code maximum. |

`JudgeBudget(0)` meaning "unlimited" was a live bug on a metered endpoint: a
caller asking for zero judge calls produced unlimited calls and an unbounded
bill. If you write code that wants "no cap", say so explicitly.

---

## 8. Configuration that is actually read

Settings that exist but were ignored have been wired up, because a setting that
does nothing is worse than no setting:

| Setting | Now read by |
|---|---|
| `DB_POOL_MIN` / `DB_POOL_MAX` | `postgres_repo._get_pool` (was hardcoded 1/6) |
| `DB_STATEMENT_TIMEOUT_MS` | `postgres_repo` (was hardcoded `15s`) |
| `LOG_JSON` | `core/logging.py` (JSON was unconditional) |
| `CORRELATION_HEADER` | `api/deps.py` (was the literal `X-Request-ID`) |

Still declared-and-unread: the `Later` phase gates (`FEATURE_DOCLING`,
`FEATURE_BROWSER`, `FEATURE_MCP`), the Qdrant settings (the flag raises rather
than uses them), `LANGSMITH_*`, and the Supabase keys (the REST client was
removed). Deleting them is a deliberate future step; leaving them is harmless
because nothing reads them.

---

## 9. Testing

```powershell
# backend -- the suite; see AGENTS.md for the markers and the full gate
cd backend; .venv\Scripts\python.exe -m pytest -q

# frontend
cd frontend; npm test; npm run build

# regenerate the contract after any route change
backend\.venv\Scripts\python.exe backend\scripts\gen_contract.py --write
```

The count is deliberately not written down. This file said 1313, `README.md` said
389 and `AUDIT.md` said 143; every one of them was stale by the time it was read,
and a wrong number is worse than none because it looks authoritative.

### The guard that matters most

`tests/test_name_binding.py` is a static scope resolver over every module in
`app/`. It finds **any** name read in a scope that never binds it — not just the
`*_svc` convention an earlier guard matched.

Two `NameError` 500s shipped past a green suite because the previous guard
only looked for `foo_svc` names, and `re` and `dependency` are not service-
shaped. The general rule found a third (`Any` used unimported in `extractor.py`)
the first time it ran.

It needs no imports to execute, so an `ImportError` elsewhere cannot hide it.

---

## 10. Frontend notes

| Fix | Why it mattered |
|---|---|
| SSE via `fetch`, not `EventSource` | `EventSource` cannot send `X-API-Key`. Every keyed install had a run stream that never connected while the header said the run was live. A `?api_key=` query param would leak the credential into logs. |
| Export returns one shape | The backend returns `{data:{format,content}}` for every format. Asking for a Blob on csv/md saved the envelope JSON as `.csv`. |
| `api.profile(id, undefined, o)` | Options were passed into the query-param slot: `?signal=[object AbortSignal]`, and the request was not abortable. |
| `overwrite` moved into the body | It was spread into fetch options and silently discarded, making the checkbox decorative. |
| `data-density` on `<html>` | The toggle persisted `dg_dense` that nothing read — a control that looked actionable and was not. |

---

## 11. Consent model

The crawler identifies itself honestly (`DATAGOBLIN/0.1 (permitted-sources bot)`),
honours `robots.txt`, and gates TLS impersonation behind an explicit allowlist
with an audit trail (`services/impersonation.py`).

This is a deliberate constraint, not a missing feature. Automated access that
misrepresents its identity is blocked faster and is harder to defend if asked
about. The performance work went into parsing, blocking, caching and determinism
instead of concealment — the deduper went from 70 s to 1.3 s on 5,000 records
without touching a single request header.

---

## 12. Where the remaining risk is

Honest inventory, not a summary of successes:

- **Selector maintenance.** `selector_learn` proposes; nothing yet re-derives and
  verifies a selector when a host's yield collapses. Reputation detects the
  collapse; the repair loop does not exist.
- **XHR capture.** Crawl4AI renders with no network-response capture, so a
  JS-heavy app's internal JSON endpoint is never discovered. `jsonrung` fires on
  JSON that is *fetched directly*, not on JSON found during rendering.
- **Blocking-key recall.** Dedup blocking can miss a duplicate whose name shares
  no 3-character prefix or 4-character suffix with any other. Documented in
  `deduper._block_keys`; a deliberate trade for a 50× speedup.
- **Staleness automation.** Ages are computed and ordering is provided; nothing
  schedules a refresh on a timer yet. `refresh_order` is advisory until something
  calls it.