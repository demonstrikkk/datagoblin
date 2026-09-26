# 33 — COMPLETE BACKEND ARCHITECTURE (lean freeze: GENERATE→ORCHESTRATE→JUDGE→EXECUTE→PROVE)

MVP = 12 (FastAPI, Supabase, Gemini, Groq, Instructor+Pydantic, Tavily, HTTPX, Trafilatura+BS4, Crawl4AI, LangGraph, Jev, RapidFuzz). Later adapters dotted, never in MVP path. No stealth/proxy/bypass ever.

## 1. Full backend drawing (mermaid — copy into any mermaid renderer)

```mermaid
flowchart TB
    subgraph CLIENT[Vite React Data Studio]
        UI_NEW["/new<br/>PromptComposer + PlanPreview"]
        UI_RUN["/runs/:id<br/>RunTimeline + SourceList + ActivityFeed"]
        UI_DS["/datasets/:id<br/>DatasetTable + ProofDrawer + FilterBar"]
        UI_HIST["/history<br/>HistoryList + ExportMenu"]
    end

    subgraph API[FastAPI control plane — backend/app/main.py]
        C[POST /api/workflows/compile<br/>prompt → WorkflowPlan]
        R[POST /api/runs<br/>plan_id → run_id]
        S[GET /api/runs/:id + /stream SSE<br/>14 event types]
        X[POST /api/runs/:id/cancel]
        D[GET /api/datasets + /:id<br/>/:id/records /:id/sources]
        E[POST /api/datasets/:id/export<br/>CSV JSON +provenance]
        H[GET /api/history]
    end

    subgraph PLAN[Plan compiler — providers/llm/generate.py]
        GEM[Gemini structured<br/>primary]
        GROQ[Groq strict JSON<br/>fallback]
        INST[Instructor → Pydantic<br/>WorkflowPlan schema]
        GEM -->|429/err| GROQ
        GEM --> INST
        GROQ --> INST
    end

    subgraph RUN[Deterministic Runner — services/runner.py<br/>ONLY stage owner<br/>GENERATE Gemini/Groq → ORCHESTRATE LangGraph → JUDGE Jev → EXECUTE code → PROVE evidence]
        direction TB
        ST0[PLANNING → DISCOVERING]
        AG[AGENTIC SUPERVISOR — LangGraph moves state<br/>SEARCH → COLLECT → ask_jev → REFINE/FETCH<br/>≤3 iterations — Jev family D judges]
        FE[FETCH MVP — HTTPX → Crawl4AI only<br/>retry once → alternate → skip<br/>Jev-A screens pre-fetch]
        RD[REDUCE — Trafilatura 30k cap]
        EX[EXTRACT — Gemini/Groq + Pydantic<br/>records + verbatim quotes]
        JV[JUDGE — Jev CORE 4 families<br/>A screen B evidence C conflict D continuation<br/>probabilities + policy → status]
        VA[VALIDATE — guard + Jev-B<br/>verified/unverified/conflicting]
        NO[NORMALIZE — original + normalized]
        DD[DEDUP — RapidFuzz L1/L2 STOP<br/>L3 Later]
        EVPV[FINALIZE — Evidence wrap → PROVE]
        ST0 --> AG --> FE --> RD --> EX --> JV --> VA --> NO --> DD --> EVPV
    end

    subgraph AGD[Supervisor loop — lean]
        direction LR
        AN[SEARCH + COLLECT<br/>Tavily ≤3 queries/iter]
        GS[JEV coverage?<br/>sufficient/insufficient/uncertain]
        ES[REFINE → SEARCH<br/>≤3 iterations total]
        AN --> GS -->|sufficient → FETCH| FE
        GS -->|insufficient → REFINE| ES
    end

    subgraph PROV[Provider mesh — MVP solid, rest Later dotted]
        TAV[Tavily MVP]
        HTTP[httpx + BS4 MVP]
        TRAF[Trafilatura MVP]
        C4[Crawl4AI MVP fallback]
        DOC[Docling Later 1.5]
        JIN[Jina Later 1.5]
        BRW[Browser Later 3]
    end

    subgraph DB[(Supabase Postgres — app truth<br/>JSONB + RLS)]
        WF[(workflows<br/>prompt plan_json)]
        RU[(runs<br/>status stage progress error)]
        REV[(run_events<br/>stage message meta ts)]
        SO[(sources<br/>url title hash status)]
        DS[(datasets<br/>schema_json counts)]
        REC[(dataset_records<br/>row_json provenance)]
        EXP[(exports<br/>format payload)]
        WF --> RU --> REV
        RU --> SO
        RU --> DS --> REC --> EXP
    end

    subgraph OPT[Optional / later]
        QDR[(Qdrant/LanceDB L3<br/>similarity index only)]
        DUCK[(DuckDB + Polars<br/>analytics export)]
        MCPX[MCP server Phase 3<br/>expose tools]
    end

    UI_NEW --> C
    C --> PLAN
    PLAN --> R
    R --> RUN
    AG -.-> AGD
    AG -.-> TAV
    FE -.-> PROV
    DD -.->|L3 Phase 2| QDR
    EVPV --> DB
    DB --> D
    D --> UI_DS
    S --> UI_RUN
    H --> UI_HIST
    DB -.-> OPT
```

## 2. Module → file map

| Layer | Files |
|---|---|
| API | `backend/app/main.py` (11 endpoints + SSE EventSourceResponse ping 15) |
| Config/constants | `core/config.py` (caps 5/5/15/2, 50/5/20s/2), `core/constants.py` (11 states, 14 events), `core/logging.py` |
| Schemas | `schemas/plan.py` (WorkflowPlan/ProvenanceField/RunEvent mirror contracts), `schemas/base.py` (Search/PageFetcher/LLM/Dedup protocols) |
| Planner/LLM | `providers/llm/generate.py` (Gemini→Groq fallback, identity stamps) + `providers/llm/langchain_client.py` (supervisor structured output seam) |
| Planner | `services/planner.py` (Instructor `from_provider` + `response_model` + `max_retries` primary; direct-SDK second; marked fallback last) |
| Discovery | `services/discovery.py` (run_id-stamped, Tavily include_domains from seed/allowed, drives `run_supervisor`) |
| Triage/fetch | `services/source_router.py` (scheme allowlist; full SSRF in fetcher) + `providers/crawl/fetcher.py` (pooled httpx, robots cache+delay, per-host throttle, backoff, content-type guard, sha256 content_hash, same-host link extractor) + `services/crawler.py` (backoff-alternate-skip, depth<2 + per-domain + global budgets, progressive persist with hash) |
| Agentic | `agents/state.py` (reducers incl. last_searched), `agents/decisions.py` (validated SupervisorDecision), `agents/tools.py` (6 shims), `agents/policies.py` (budgets), `agents/graph.py` (search→screen→decide→refine/fetch; compiled LangGraph w/ InMemorySaver+thread_id or identical manual stepping; no-progress exit) |
| Decisions | `providers/decision/jev.py` (CORE 4 families + real `/v1/systemone` path + deterministic stubs) |
| Pipeline | `services/reducer.py`, `services/extractor.py` (capped, envelope-validated), `services/validator.py` (RecordRow-guarded wrap), `services/normalizer.py`, `services/deduper.py` (L1→L2, rivals preserved, `adjudicate_conflicts` JUDGE step), `services/provenance.py`, `services/exporter.py` (caps, CSV-injection guard), `services/runner.py` (JUDGE block emits stats, runtime-capped, cancel-aware) |
| DB | Supabase tables per §DB above (sources now carry content_hash; exports rows written per download) + `local_repo.py` dev mirror; in-memory dicts in main.py = Day-1 swap target |

## 3. Runtime path (one run)

compile → plan → POST /runs → DISCOVERING (Tavily ≤5q, triage each URL, Jev-A screens pre-fetch) → FETCHING (MVP http→crawl4ai; Later +docling/jina/browser) → REDUCING → EXTRACTING (LLM+quotes) → JUDGE (Jev-B evidence, Jev-C conflicts) → VALIDATING (guard+policy → verified/unverified/conflicting) → Jev-D continuation inside supervisor loop (≤3 iters) → NORMALIZE → DEDUPLICATING (L1/L2 STOP; L3 Later) → FINALIZING (persist + counts attempted/successful/failed) → COMPLETED/FAILED/CANCELLED. Every step emits SSE; cancel preserves partial; all-fail → FAILED, some-fail → partial.
