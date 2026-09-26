# 05 — SYSTEM ARCHITECTURE (modular monolith)

```
              Vite React (Data Studio)
                       │
                   REST + SSE
                       │
                    FastAPI (control plane)
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Planner     Run Engine    History/Export
       (LLM +         (state        (Supabase)
        Pydantic)     machine +
                      retry)
          │            │
          │       DISCOVERY (Tavily)
          │       FETCH (HTTP → Crawl4AI)
          │       REDUCE (trafilatura/Markdown)
          │       EXTRACT (LLM+Instructor)
          │       VALIDATE (Pydantic+evidence)
          │       DEDUP (RapidFuzz [+Qdrant opt])
          │              │
          └──────────────┤
                         ▼
                   Supabase (workflows, runs, datasets, records, sources, run_events)
                   Optional: Qdrant (similarity index only), DuckDB (analytics/export later)
```

Backend layout (exact):

```
backend/app/{main.py, api/{routes_runs,routes_datasets,routes_history,routes_export}.py,
core/{config,logging,constants}.py, schemas/{plan,run,dataset,provenance}.py,
services/{planner,discovery,crawler,reducer,extractor,validator,normalizer,deduper,provenance,runner,exporter}.py,
providers/{llm/{gemini,groq}.py, search/tavily.py, crawl/{http,crawl4ai}.py},
repositories/{workflows,runs,datasets,sources}.py, events/stream.py}
```

Boundary: LLM does not control application execution. Runner owns stages, retries, progress, storage.
Provider interfaces: `SearchProvider.search`, `PageFetcher.fetch`, `LLMProvider.structured_generate`, `DedupEngine.deduplicate`.
