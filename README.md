# DATAGOBLIN — ask real questions, get verified answers

A research backend that answers a natural-language question by searching the web,
crawling what it finds, and returning a dataset where **every non-null field cites
the exact stored text it came from** — or the field stays null.

Empty results are honest. Unverified is never quietly reported as verified.

**Current state, measured:** [`docs/37-CURRENT-STATE.md`](docs/37-CURRENT-STATE.md)
— what works, what is still broken.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
  - [The pipeline](#the-pipeline)
  - [The evidence store](#the-evidence-store-the-core-design)
  - [Who decides what](#who-decides-what)
  - [Persistence](#persistence)
  - [Concurrency and the runtime budget](#concurrency-and-the-runtime-budget)
- [Repository layout](#repository-layout)
- [API](#api)
- [Data model](#data-model)
- [Quickstart](#quickstart)
- [Free-model intel](#free-model-intel-intel)
- [Configuration](#configuration)
- [Tests](#tests)
- [Honest limitations](#honest-limitations)
- [Security posture](#security-posture)
- [Docs](#docs)

---

## What it does

```
"List AI companies in Europe that publish annual revenue figures"
        │
        ▼
  a dataset of companies, each field carrying a quote and a link
  to the stored page it was read from
```

It is a **deterministic** pipeline with **bounded agentic** parts, and a
proof-first rule: no claim without a stored citation.

---

## Architecture

### The pipeline

```
                         USER PROMPT
                              │
                    ┌─────────▼─────────┐
                    │  PLANNER  (LLM)   │  goal · fields · search_queries
                    └─────────┬─────────┘  dedupe_keys · max_pages
                              │            required capped at 2
                    ┌─────────▼─────────┐
                    │  TAVILY  (search) │  candidate URLs
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  JEV-A  screen    │  NO ⇒ skip fetch+extract entirely
                    └─────────┬─────────┘
                              │
        ┌─────────────────────▼─────────────────────┐
        │  CRAWLER   4 workers, depth-limited BFS    │
        │  HTTPX+BS4 ─► [Crawl4AI/Playwright for JS]  │
        │  robots.txt · per-host throttle · backoff   │
        └─────────────────────┬─────────────────────┘
                              │
              ════════════════▼═══════════════════════
              ║   STORE — a `pages` row per page     ║  ◄── the substrate
              ════════════════╤═══════════════════════
                              │
                    ┌─────────▼─────────┐
                    │  REDUCER          │  page_evidence_text()
                    │  (one definition)  │  quotable text, incl. media + JSON-LD
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  EXTRACTOR  (LLM) │  chunks CONCURRENT × pages CONCURRENT
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  VALIDATOR        │  type · required · quote-substring
                    │  (a real gate)    │  ⇒ Jev-B · drops bad records
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  DEDUPER  L1+L2   │  ⇒ Jev-C conflict adjudication
                    └─────────┬─────────┘
                              │
              ════════════════▼═══════════════════════
              ║   STORE — dataset + records, atomic  ║
              ════════════════╤═══════════════════════
                              ▼
              DATASET  →  table · proof drawer · export
```

The research loop (Jev-D) closes around **discovery** only — see
[Who decides what](#who-decides-what).

### The evidence store: the core design

This is the part that had to be right, and it was wrong first.

The original pipeline was:

```
CRAWL ──► RAM ──► EXTRACT ──► discard the page
```

A page was fetched, reduced in memory, extracted from, and thrown away. Only a
SHA-256 and a character count survived. Every evidence quote carried `start`/`end`
offsets **into text that no longer existed**, so no record could ever be
re-checked and `content_hash` could never be confirmed against anything.

The correct order is:

```
CRAWL ──► STORE ──► process the stored page ──► EXTRACT ──► VALIDATE
```

| column in `pages` | why it exists |
|---|---|
| `markdown` | the exact reduced text the LLM saw — quotes resolve against **this** |
| `raw_html` | the snapshot, for re-checking `content_hash` |
| `content_hash` | re-fetch and confirm the page has not changed under a claim |
| `parent_url`, `depth` | the crawl tree is reconstructable |
| `retrieved_at` | when this evidence was collected |
| `id` | **cited by records** via `source.page_id` |

The single most important implementation detail: **`reducer.page_evidence_text()`
is the one definition of "quotable text on this page", called by both the store
and the extractor.** They used to disagree, and any difference between them makes
a verified quote unverifiable against the stored page — which defeats the point
of storing it.

A test asserts the round trip end to end: take a record, take its cited
`page_id`, slice the stored `markdown` at the cited offsets, and require it to
equal the quote. **On the last live run: 226/226 resolved, 0 failures.**

### Who decides what

```
LLM    = understand / generate      plan, extract
Jev    = judge / decide             is this source relevant? does this quote
                                    support this claim? which value wins?
                                    is the coverage enough?
Python = execute                    applies the policy, mutates state
DB     = remember
```

Jev returns a judgement with probabilities. **Python applies the threshold.**
Jev never writes anything and never decides what gets stored. Where a judge is
unavailable, every family degrades to a stated deterministic outcome rather than
a fabricated judgment.

| family | call | unreachable ⇒ |
|---|---|---|
| **A** source screening | `jev.source_screening()` | plural-tolerant entity match |
| **B** evidence verification | `jev.evidence_verification()` | `judgment_unavailable` — **never** "verified" |
| **C** conflict resolution | `jev.conflict_triage()` | `CONFLICT` — never merged |
| **D** research continuation | `jev.research_continuation()` | count comparison |

> Family B used to return `SUPPORTED` whenever no judge was reachable, so a dead
> or unconfigured judge silently promoted every substring match to "verified" —
> and because no test stubbed the judge, **the entire suite was passing on that
> bug**. Family D was documented as a coverage judge and was a no-op that made no
> model call at all. Both are now real, with tests.

### Persistence

Selected explicitly, never inferred:

| `PERSISTENCE` | backing store | when |
|---|---|---|
| `postgres` *(default)* | Supabase Postgres via **psycopg** | `DATABASE_URL` is set |
| `local` | JSONL under `.datagoblin-local/` | explicit opt-in, no infrastructure |

The active adapter is reported by `GET /api/health`, so a silent downgrade cannot
go unnoticed:

```json
{"data":{"status":"ok","persistence":{"adapter":"postgres","detail":"Postgres via psycopg"}}}
```

The adapter used to be inferred from whether Supabase keys were present, and their
absence quietly dropped the app to JSONL while a fully-migrated, empty database
sat unused with nothing reporting it.

### Concurrency and the runtime budget

The budget is **per stage and per page**, not one wrapper around the pipeline. A
slow discovery phase can no longer consume the whole allowance and leave
fetch/extract/validate unrun.

Three fixes did the throughput work, all measured:

| was | now |
|---|---|
| extraction chunks ran **sequentially** — a page cost the *sum* of its calls, so 4 chunks could not fit inside `EXTRACT_PAGE_TIMEOUT_S` and **every page returned 0 records** | concurrent; the cost is the *slowest* chunk |
| pages extracted **one at a time** — 8 pages could not finish inside the budget | concurrent, bounded at 3, rate-limit pacing kept and applied in *waves* |
| Jev-C conflicts adjudicated **one at a time** — 35 duplicates ate the remaining budget | concurrent |

Partial results are a real, queryable outcome. Records are published per page and
stored on timeout; the run is marked `FAILED` with `partial = true`, and the
reported count is the count actually kept — **including zero**, with a message
that says so. The old code emitted `"partial kept"` while `store()` was never
reached, discarding every record.

---

## Repository layout

```
backend/app/
  main.py                    FastAPI app · 16 endpoints · SSE · event→state
  core/       config.py      every cap, key, timeout, feature flag
              constants.py   RunStage · legal transitions · emitted event set
              errors.py      typed AppError (fatal / transient / validation / dependency)
              logging.py     structured JSON logging
  api/        deps.py        correlation id · optional API-key gate
  agents/     state.py       explicit TypedDict supervisor state
              graph.py       search → screen → decide (≤3 iterations)
              decisions.py   validated SupervisorDecision
              policies.py    query budget
              tools.py       DEAD CODE — no production importer
  services/   runner.py      THE stage owner
              planner.py     prompt → WorkflowPlan (required capped at 2)
              discovery.py   drives the supervisor loop
              crawler.py     fetch waterfall · BFS traversal · STORES pages
              reducer.py     page_evidence_text() ← the one definition
              extractor.py   LLM structured extraction, concurrent
              validator.py   the gate · type/required/judgment budget
              item_pipeline.py  bounded per-record stages with DropItem
              normalizer.py  deterministic value coercion
              deduper.py     L1 + L2 · rivals preserved
              fanout.py      free-model fan-out · agreement structure
              politeness.py  robots · throttle · JS-shell detection
              selectors.py   deterministic per-domain extraction (runs first)
              metering.py    credit projection and clamping
              provenance.py  record/field-level honest counting
              exporter.py    CSV / JSON / JSONL / Markdown
              jobs.py        job status view
              source_router.py  pure string triage — no LLM
  providers/
    search/   tavily.py      the only search backend
    crawl/    fetcher.py     httpx · Crawl4AI · Jina · media/link extraction
              docling.py     no callers — only a route string and a comment
              file_fetch.py  local-corpus replay (tests/scripts)
    llm/      generate.py    the LLM seam
              opencode.py    v1 wire protocol · session pool · honest provenance
              zen.py         free-model registry · per-model transport dispatch
              classify.py    shared transient/fatal classification
              langchain_client.py  supervisor structured output (strict mode skips it)
    decision/ jev.py         the 4 judge families + deterministic fallbacks
  repositories/
    factory.py               explicit PERSISTENCE switch
    postgres_repo.py         psycopg · one COPY for records · atomic finalize
    local_repo.py            JSONL, explicit opt-in
  schemas/    plan.py · run.py · evidence.py · base.py
  events/     stream.py      per-run event bus

frontend/src/
  App.jsx                   routes
  routes/      New · Run · Dataset · History · Intel
  components/  workspace.jsx · studio.jsx
  hooks/       useRunStream.js
  lib/         api.js        envelope unwrapping · error surfacing

backend/migrations/  001_core.sql · 002_phase5.sql · 003_evidence.sql
```

---

## API

16 endpoints, all responding `{data, error, meta}` — including validation
errors, which used to leak FastAPI's `{"detail":[...]}` and rendered as a blank
error in the UI.

| method | path | purpose |
|---|---|---|
| GET | `/api/health` | liveness **+ the active persistence adapter** |
| POST | `/api/workflows/compile` | prompt → plan (returns `plan_id`) |
| POST | `/api/runs` | start a run from a `plan_id` |
| GET | `/api/runs/{id}` | run state, `partial` flag, counters |
| GET | `/api/runs/{id}/stream` | SSE progress (13 event types) |
| POST | `/api/runs/{id}/cancel` | cancel a run |
| GET | `/api/jobs/{id}` | job status view |
| GET | `/api/history` | run history |
| GET | `/api/datasets` · `/{did}` | dataset list / detail |
| GET | `/api/datasets/{did}/records` | records, with SQL-side filter + paging |
| GET | `/api/datasets/{did}/sources` | sources for the run |
| POST | `/api/datasets/{did}/export` | CSV / JSON / JSONL / Markdown |
| POST | `/api/map` | single-URL triage |
| GET | `/api/models/free` | free-model registry + live reachability |
| POST | `/api/intel/ask` | fan a question across the free tier |

`plan_id` survives a restart: plans are persisted, not held in a process dict.

---

## Data model

```
runs          status · stage · progress · query · partial · error · stats
 ├─ sources   per-URL metadata (no body)
 ├─ pages     ── THE EVIDENCE ── markdown · raw_html · content_hash · depth
 ├─ run_events  stage · message · metadata        (see gap: no `type` column)
 └─ datasets  ── dataset_records ── row_json { fields: { value, verification_status,
                                                    source { url, quote, start, end,
                                                             page_id, content_hash } } }
workflows     prompt + plan_json
usage_ledger  credit metering (idempotent on charge_id)
```

Verification statuses: `verified` · `unverified` · `conflicting` ·
`judgment_unavailable`.

`judgment_unavailable` is deliberately not `verified`: the quote *is* on the
page, but **nothing ruled on it**. That distinction is the whole point.

Full schema: [`docs/14-DATABASE-SCHEMA.md`](docs/14-DATABASE-SCHEMA.md).

---

## Quickstart

Backend — from the **repo root**, because the server reads `.env` from the CWD:

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cd ..
copy .env.example .env                       # fill keys (docs/23-ENVIRONMENT.md)
python backend/scripts/db_migrate.py         # apply the schema (idempotent)
set PYTHONPATH=backend && python -m uvicorn app.main:app --port 8000
```

`copy` works in `cmd` and PowerShell; `Copy-Item` also works.

Local OpenCode server — needed for the free model tier **and** for extraction:

```bash
set OPENCODE_SERVER_PASSWORD=<the OPENCODE_PASSWORD value in your .env>
opencode serve --port 4096 --hostname 127.0.0.1
```

> A server started *without* `OPENCODE_SERVER_PASSWORD` accepts any password,
> including none — so a password in `.env` protects nothing unless the server is
> launched with it. `start-dev.ps1` starts all three processes and forwards it.

Frontend:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173  (/api proxied to :8000)
```

Then: ask → compile → plan preview → **Run** → live SSE timeline → dataset →
proof drawer → export.

---

## Free-model intel (`/intel`)

One Zen key reaches the whole free model tier, but **not over one transport** —
measured, not assumed. On direct REST only `space-bunny-free` answers; the others
return `403 FreeTierError` and are reachable only through a running
`opencode serve`. Each registry entry therefore carries its transport and the code
dispatches accordingly. Nothing brute-forces the gate.

```bash
opencode auth login                                  # stores the Zen key
OPENCODE_SERVER_PASSWORD=<same> opencode serve --port 4096
```

| endpoint | what it does |
|---|---|
| `GET /api/models/free` | registry: every free model, transport, live reachability |
| `POST /api/intel/ask` | fan one question (or one URL) across the tier |

A URL is fetched server-side through the same robots/rate-limit waterfall a run
uses. Each model fails independently: intermittent upstream 500s return as
per-model errors beside the answers that did land, never as an empty batch.

`consensus` reports **agreement structure only** — models agreeing is not
evidence. It counts *distinct* models that actually answered, because a
mis-pinned transport can serve four requests from one model and make 4/4 look
like unanimity. A single source is labelled `single-model`,
`independent: false`.

> `GET /zen/v1/models` is deliberately not used to build the registry: it lists 82
> models with null cost, so "free" cannot be derived from the payload.

---

## Configuration

Every cap, key, timeout and flag lives in `backend/app/core/config.py`. The ones
that change behaviour most:

| setting | default | note |
|---|---|---|
| `PERSISTENCE` | `postgres` | `local` for zero-infrastructure |
| `DATABASE_URL` | — | read by the app; the only DB credential needed |
| `OPENCODE_MODEL` | — | **the extraction model; its latency is the run's latency** |
| `RUN_MAX_RUNTIME_S` | 1200 | checked per stage *and* per page |
| `RUN_MAX_PAGES` | 12 | global page budget |
| `RUN_MAX_DEPTH` | 2 | **hops below the seed** |
| `EXTRACT_PAGE_CONCURRENCY` | 3 | pages extracted at once |
| `RUN_MAX_JUDGE_CALLS` | 400 | past this, fields are `judgment_unavailable` |
| `EXTRACT_PAGE_SPACING_S` | 10 | free-tier rate-limit pacing |
| `FETCH_THIN_CHARS` | 500 | below this + scripts ⇒ escalate to a renderer |

Measured extraction latency on a realistic 21.5k-character prompt:

| model | latency |
|---|---|
| **big-pickle** | **4.9s** |
| muse-spark-1.3-contributor-free | 29.2s |
| nemotron-3.5-lightning-free | 162.1s |
| mimo-v2.6-flash-free | 170.2s (timed out) |

`OPENCODE_MODEL` is currently `opencode/big-pickle`. Against a 150s page timeout
the slower models simply time out and the page returns zero records — **re-measure
before changing it**, since a faster model extracts more records, which moves cost
into validation. Details: [`docs/23-ENVIRONMENT.md`](docs/23-ENVIRONMENT.md).

---

## Tests

```bash
python -m pytest backend/tests -q          # 361 passed, hermetic, no credits
cd frontend && npm run build               # production build
```

Hermetic by construction: `.env` is ignored, every provider key is blanked per
test, and the judge is stubbed by an autouse fixture. The `no_judge` fixture opts
out to test the outage path honestly.

---

## Honest limitations

Not hidden, because "it works" is the claim that hid the most damage:

- **`run_events` has no `type` column** — the persisted audit trail cannot
  distinguish event kinds after the fact. Needs a migration.
- **PDF and JSON sources are always skipped** — Docling has zero callers and both
  routes resolve to methods that hit the non-HTML guard.
- **Jina is implemented but unreachable** — absent from the crawl order table, so
  no dispatch can select it.
- **No cross-run dedup**, and `dataset_records` has no unique index.
- **The dedupe-key fallback is weak** — an empty `dedupe_keys` degrades to "every
  field name, sorted", and the planner often emits none.
- **`REDUCING` / `FINALIZING` stages are declared but never emitted.**
- **Dead config that reads like a feature:** `QDRANT_*`, `FEATURE_BROWSER`,
  `FEATURE_MCP`, `FEATURE_DOCLING`, `RUN_MAX_REQUESTS`,
  `RUN_MAX_PAGES_PER_SOURCE`, `RUN_MAX_SEARCH_QUERIES`, `DB_POOL_*`.
- **One known flaky test**, not reproduced in isolation. Observed, not fixed.
- **No frontend browser tests** — it builds and a human confirmed it renders.

Full list: [`docs/37-CURRENT-STATE.md`](docs/37-CURRENT-STATE.md).

---

## Security posture

Permitted sources only. robots.txt honored (fetched as our own UA, cached 1h),
per-host throttle with `Crawl-delay` and adaptive throttling, exponential backoff
with jitter, a domain allowlist, and a full SSRF check in the fetcher.

Never: bypass auth, bypass paywalls, solve CAPTCHAs, credential stuffing, TLS
spoofing, proxies, or execute page JS outside a browser sandbox.

The frontend never receives server secrets. API-key auth is optional and
off by default in local dev.

> **Outstanding risk:** `.env` is currently committed-readable with live secrets
> and `.gitignore` is proposed, not present. See
> [`docs/37-CURRENT-STATE.md`](docs/37-CURRENT-STATE.md).

---

## Docs

Start with **[`docs/37-CURRENT-STATE.md`](docs/37-CURRENT-STATE.md)** — it
describes reality; the rest mostly describe intent.

| if you care about | read |
|---|---|
| does it work end to end | `37-CURRENT-STATE.md` |
| where page content is kept, and why | `29-EVIDENCE-LAYER.md` |
| what "verified" means | `11-VALIDATION-SPEC.md` |
| the stored schema | `14-DATABASE-SCHEMA.md` |
| what the frontend receives | `16-SSE-EVENT-CONTRACT.md` |
| what each judge is for | `32-JEV-USAGE.md` |
| crawl order and *real* limits | `09-CRAWLING-POLICY.md` |
| the path a run takes | `06-DATA-FLOW.md` |
| keys and measured latencies | `23-ENVIRONMENT.md` |
| the module map | `33-BACKEND-ARCHITECTURE.md` |
| known-bad audit findings, corrected | `AUDIT.md` (addendum) |

`docs/00`–`docs/36` are the build specs. `AUDIT.md` tracks dead code;
`CONSISTENCY-REPORT.md` is superseded and kept for history.

No `TODO`s ship as done work.
