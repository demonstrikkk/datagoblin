# 06 — DATA FLOW (one prompt, end to end)

```
USER PROMPT
  -> Planner (LLM) -> WorkflowPlan {fields, search_queries, dedupe_keys, max_pages}
  -> Tavily -> candidate URLs
  -> Jev-A  source screening      (NO skips fetch+extract entirely)
  -> Crawler  HTTPX+BS4 -> [Crawl4AI/Playwright for JS] -> depth-limited BFS
       -> STORES a `pages` row per settled page   <-- the evidence substrate
  -> Reducer  page_evidence_text(): the ONE definition of quotable text,
              shared by the store and the extractor
  -> Extractor (LLM) chunks CONCURRENTLY, and pages extract CONCURRENTLY
  -> Validator  types + required + quote-substring gate, then Jev-B
       -> record dropped if a required field has no evidence
  -> Deduper  L1 exact + L2 fuzzy
  -> Jev-C  conflict adjudication (concurrent)
  -> Dataset (Postgres) + run state, atomically
  -> Present (table + proof + trace) -> Export (CSV/JSON)
```

## The two properties that matter

1. **The page is stored, not discarded.** A page is written to `pages` at
   settle time, carrying the exact text the LLM saw. Every record cites that
   row via `source.page_id`, so a quote can be re-checked long after the run
   (see docs/29). Previously the pipeline was `crawl -> RAM -> extract ->
   discard` and every evidence offset pointed into memory that no longer existed.
2. **Partial is a real, queryable outcome.** If the runtime budget runs out,
   the records already validated are stored, the run is marked
   `FAILED + partial=true`, and the reason is reported. The old code emitted
   "partial kept" while `store()` was never reached, discarding everything.

## Timing

The budget is **per stage and per page**, not one wrapper around the pipeline.
A slow DISCOVERING can no longer consume the whole allowance and leave
FETCH/EXTRACT/VALIDATE unrun.

Extraction is concurrent in both dimensions — chunks within a page, and pages
within the run — because the cost used to be the *sum* of every model call, and
8 pages could not finish inside the budget however much was already done. Rate
limit pacing (`EXTRACT_PAGE_SPACING_S`) is kept, applied in waves.

## Live limits (docs/09 lists these; several entries there were dead)

| control | live setting |
|---|---|
| global page budget | `min(plan.max_pages, RUN_MAX_PAGES)` |
| per-domain pages | `min(plan.traversal.max_pages_per_domain, RUN_MAX_DOMAIN_PAGES)` |
| depth | `RUN_MAX_DEPTH` — counts hops below the seed |
| search queries | `SUPERVISOR_MAX_QUERIES` |
| results per query | `RUN_MAX_RESULTS_PER_QUERY` |
| fetch concurrency | 4 (`crawler._WORKERS`) |
| extract concurrency | `EXTRACT_PAGE_CONCURRENCY` (default 3) |
| judge calls | `RUN_MAX_JUDGE_CALLS` (default 400) |
| wall clock | `RUN_MAX_RUNTIME_S` |
