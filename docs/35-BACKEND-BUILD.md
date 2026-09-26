# 35 — BACKEND BUILD SPEC (production implementation, Phase 2/3)

## 1. Module DAG (no cycles; dependencies point inward)

```
api (main.py, deps) ──► services (runner, pipeline/*, discovery, exporter)
services ──► agents (supervisor) ──► providers/decision (jev)
services ──► providers (search, crawl, llm) + repositories (persist)
agents/state, decisions, policies: pure types (no imports from services)
core: config/constants/logging/errors (imported by all, imports none)
```

Transport owns parsing/auth/response. Business (runner+agents+pipeline) owns decisions/invariants. Persistence owns queries/transactions. Adapters own provider mapping/failure translation.

## 2. Data flow per stage (input → output → owner)

| Stage | Input | Output | Owner |
|---|---|---|---|
| compile | {prompt} | WorkflowPlan (validated) | services/planner.py + Instructor |
| supervise | ResearchState | SupervisorDecision (validated, budget-capped) | agents/graph.py + Jev-D |
| discover | queries | deduped candidate URLs + triage routes | services/discovery.py |
| fetch | URL + route | Page{url,title,markdown|html,method} or SourceFailure | providers/crawl/*, semaphore 4 |
| reduce | HTML | clean markdown ≤30k | services/reducer.py (trafilatura) |
| extract | schema+markdown | records + verbatim quotes | services/extractor.py (Gemini→Groq) |
| judge | value+quote+text | SUPPORTED/NOT_SUPPORTED/CONFLICT verdicts | providers/decision/jev.py |
| validate | records+verdicts | provenance-wrapped rows | services/validator.py |
| normalize | rows | rows + normalized{} | services/normalizer.py |
| dedupe | rows | canonical rows + merge events | services/deduper.py (L1→L2) |
| finalize | rows + counts | dataset_id (atomic write) | repositories/* |
| export | dataset_id + format | CSV/JSON bytes | services/exporter.py |

## 3. Error taxonomy (maps to HTTP + SSE + retry policy)

- `E_VALIDATION` (422): schema/plan/record shape violations. Never retried.
- `E_BUDGET` (429): query/page/runtime caps hit → graceful partial finalize.
- `E_PROVIDER_TRANSIENT` (502): timeouts, 429/5xx from Tavily/fetch/LLM → retry once → alternate method → skip source.
- `E_PROVIDER_FATAL` (401/403/paywall): no retry, no bypass — mark source blocked, continue.
- `E_DEPENDENCY` (503): Supabase unreachable → run pauses with resumable state (checkpointer) rather than data loss.
- `E_CANCELLED` (499-style): user cancel → CANCELLED + partial preserved.
- Unknown → 500 safe envelope + correlation ID; internal cause logged redacted.

## 4. Concurrency & transactions

- One asyncio.Task per run; runs isolated (no shared mutable state; stores keyed by run_id).
- Fetch semaphore(4); LLM calls sequential per page (cost control) with per-call timeout.
- Cooperative cancellation: task.cancel() + cancelled-flag checks between stages.
- Persistence is progressive: sources upserted as fetched; dataset written atomically at finalize (single transaction: dataset + records + counts).
- SSE event log capped at 2000 entries/run (drop-oldest, keep counter) — O(1) memory.

## 5. Security boundaries (OWASP-relevant)

- Inputs: plan/record/URL validated (Pydantic + URL scheme allowlist http/https, length caps, SSRF guard: no localhost/metadata-IP targets).
- Secrets: server-only, never logged, never sent to frontend (logging redacts *_KEY/*_SECRET).
- Web content untrusted: never executed, never overrides policy; quotes length-capped.
- Optional API_KEY gate on mutating endpoints; CORS explicit origins.
