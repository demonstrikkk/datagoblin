# 06 — DATA FLOW (one prompt, end to end)

```
USER PROMPT → Prompt Compiler → WorkflowPlan → Search Queries → Search Provider (Tavily)
→ Candidate URLs → Source Registry (dedupe URLs) → Fetcher (HTTP waterfall → Crawl4AI)
→ Raw Content → Content Reducer (strip script/style/nav/footer/svg/ads → main → Markdown)
→ Clean Content (+ schema + url + title) → Extractor (LLM structured)
→ Raw Records → Validator (types + required + evidence-substring guard)
→ Verified Records → Deduper (exact → fuzzy → semantic opt) → Canonical Records → Dataset (Supabase)
→ Present (table + proof + trace) → Export (CSV/JSON)
```

Hard limits: `MAX_SEARCH_QUERIES=5`, `MAX_RESULTS_PER_QUERY=5`, `MAX_PAGES=15`, `MAX_DEPTH=2`.
Fetcher priority: direct HTTP first, Crawl4AI on JS-heavy/failure, Jina Reader as documented fallback only.
