# DATAGOBLIN — Detailed System Architecture (authoritative companion to docs/05)

Source: user blueprint §§1–9, 28–43 + docs/00–27 + skills/* (live-verified 2026-09-24). No invented infra.

## 1. Logical layers

```
Browser (Vite React JSX + Tailwind, Fieldwork theme)
  │ REST (JSON) + SSE (text/event-stream)
  ▼
FastAPI control plane (modular monolith)
  ├─ Planner (LLM→Pydantic via Instructor; Gemini primary → Groq fallback; LiteLLM later)
  ├─ Run Engine (deterministic state machine PLANNING→…→FINALIZING→COMPLETED; ANY→FAILED/CANCELLED)
  │    ├─ Discovery (Tavily 3–5 queries × max_results 5 → dedupe URLs, cap MAX_PAGES 15)
  │    ├─ Fetch (httpx+BS4 → trafilatura text; fail/JS-heavy → Crawl4AI fit_markdown; → Jina r.jina.ai fallback)
  │    ├─ Reduce (strip script/style/nav/footer → trafilatura markdown → token cap → clean content)
  │    ├─ Extract (schema+url+title+markdown → Instructor records+verbatim quotes; page text untrusted)
  │    ├─ Validate (Pydantic types + required + evidence-substring guard → verified|unverified|conflicting)
  │    ├─ Dedup (L1 exact-normalized → L2 RapidFuzz token_set → L3 FastEmbed+Qdrant optional)
  │    └─ Finalize (Supabase persist + counters + export payload)
  ├─ Providers (interfaces: SearchProvider.search / PageFetcher.fetch / LLMProvider.structured_generate / DedupEngine.deduplicate)
  ├─ Repositories (Supabase Postgres = truth; JSONB rows; Qdrant = index only; DuckDB = later export)
  └─ Events (SSE: 14 types; timeline + counters + activity feed)
```

LLM owns ONLY plan-shape + extraction-shape. Code owns stages, retries, validation, dedupe, progress, storage, export.

## 2. Request lifecycle (happy path + partial)

POST /api/workflows/compile {prompt} → WorkflowPlan (planner-system.md) → UI PlanPreview → POST /api/runs {plan_id}
→ run PLANNING→DISCOVERING (Tavily) → FETCHING (waterfall per URL: retry once → alternate method → skip) → REDUCING
→ EXTRACTING → VALIDATING → DEDUPLICATING → FINALIZING (persist dataset+sources+events) → COMPLETED.
GET /api/runs/{id}/stream emits stage.started/progress, source.discovered/fetched, record.extracted/verified/rejected, duplicate.*.
Cancel → CANCELLED + partial dataset preserved. All-fail → FAILED + error. Some-fail → partial + attempted/successful/failed.

## 3. Data contracts

WorkflowPlan: contracts/workflow-plan.schema.json (goal, entity, requested_count≤50, fields[name snake, type string|number|boolean|date|array|url], 1–5 queries, dedupe_keys, max_pages≤15, allowed_sources public-only).
Provenance field: {value, verification_status, source{url,title,quote⊂source_text,retrieved_at}}.
RunEvent: {type∈14, run_id, stage∈11, message, progress 0–100, timestamp, data}.
Dataset row: row_json = {field: provenance-field}. Schema doc separate (schema_json). Export adds _source_url/_verification_status/_retrieved_at.

## 4. Storage topology

Supabase tables: workflows(prompt, plan_json) → runs(status, current_stage, progress, error) → run_events → sources(url,title,hash,status) → datasets(schema_json, counts) → dataset_records(row_json) → exports. RLS on, secret key server-only (skills/supabase). Qdrant collection per entity (id→vector, COSINE) keyed by Postgres id. DuckDB reads exports for analytics (future).

## 5. Non-functional envelopes

Caps: MAX_SEARCH_QUERIES 5, MAX_RESULTS_PER_QUERY 5, MAX_PAGES 15, MAX_DEPTH 2, max_requests/run 50, max_pages/source 5, timeout 20s, retry 2. SSE ping 15s, no-store. Crawl4AI heavy (Chromium), cache BYPASS. Tavily basic=1 credit. Free tiers quoted in skills, re-check at deploy.

## 6. Failure matrix → docs/19. Security → docs/20 (page content never overrides policy). Demo replay → docs/25 (stored plan+material plays visually on live failure).

## 7. Evolution (docs/26): monolith → +worker/Redis → +queue/pools → Temporal/K8s/ClickHouse/object-store. No code changes to domain layer required (provider interfaces).
