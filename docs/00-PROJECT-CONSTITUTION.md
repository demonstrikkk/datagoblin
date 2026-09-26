# 00 — PROJECT CONSTITUTION (DATAGOBLIN — Source of Truth, Highest Priority)

> BEFORE IMPLEMENTING ANYTHING, read docs 00,01,02,03,05,07,08,14,15,17,18,20.
> Treat these files as source of truth. Do not invent architecture, APIs, DB fields,
> workflow states, UI routes, technologies, dependencies, or requirements.

## 1. Product (what it is)

DATAGOBLIN converts a natural-language business data requirement into a
source-backed structured dataset.

Core flow (exact order — FROZEN):

```
Prompt → Plan → Discover → Source Triage → Fetch → Reduce → Extract → Validate → Normalize → Deduplicate → Evidence → Store → Present → Export
```

Fetch hierarchy — MVP (lean): `HTTPX → Crawl4AI` (+Trafilatura reduce). Later adapters
(not MVP): Docling documents, Jina difficult-pages, BrowserFetcher Stagehand/BrowserUse (last resort).

Product idea: natural-language requirement → dynamically generated collection
workflow → permitted web sources → structured + validated data → source proof →
interactive dataset → history/export.

## 2. Architectural philosophy

- Modular monolith for the hackathon (FastAPI + Vite React). Clean module
  boundaries, no microservices.
- Controlled AI planner + deterministic workflow engine. LLM produces a
  `WorkflowPlan`; application code executes it. **LangGraph owns bounded agentic research decisions; the DATAGOBLIN Runner owns application execution.** One Research Supervisor subgraph only — never LangGraph-everywhere, never 10 agents.
- Three intelligences: GENERATE (Gemini/Groq — what to extract) → ORCHESTRATE (LangGraph — research loop) → JUDGE (Jev — is this good/true/enough) → EXECUTE (Python Runner) → PROVE (evidence).
- Provider-agnostic execution layer: Search / Fetch / LLM / Dedup behind
  interfaces so Tavily→Brave/Serper, Gemini→Groq/OpenAI, HTTP→Crawl4AI→future
  browser service are swappable without rewriting domain logic.
- Product metaphor: Data Research Compiler, not "AI scraper". Crawlers, search,
  LLMs, Qdrant, Supabase are backends; product is orchestration + provenance +
  dataset experience.

## 3. Deferred beyond MVP unless scale justifies (NOT "never" — evolution in 26)

Do NOT introduce in MVP (deferred, not banned):
- microservices, Kubernetes / EKS, Temporal / durable executors, ClickHouse / Aurora, Kafka / Redis Streams (except optional Upstash later), worker pools
- complex autonomous agent loops / LangGraph everywhere / unlimited browser actions
- residential proxies, TLS fingerprint spoofing, CAPTCHA bypass, paywall bypass, credential collection
- billing / Stripe, RBAC, enterprise SSO, teams, scheduling engine, programmatic SEO
- MinHash-at-scale, huge vector pipeline (Qdrant is optional similarity index only)

See `02-MVP-SCOPE.md`, `03-NON-GOALS.md`, `26-FUTURE-ARCHITECTURE.md`.

## 4. Frontend (fixed)

- Vite + React JSX + Tailwind. shadcn/ui or lightweight custom components allowed.
- Do NOT use: Next.js, TSX, NextResponse, next/navigation, NextAuth.

## 5. Backend (fixed)

- FastAPI + Python. Pydantic for validation. SSE for run streaming (not WebSockets).
- Supabase Postgres for app truth. JSON/JSONB for dynamic records (no rigid per-entity SQL tables).
- See `05-SYSTEM-ARCHITECTURE.md`, `14-DATABASE-SCHEMA.md`, `15-API-CONTRACT.md`.

## 6. AI responsibilities (hard boundary — FROZEN)

**LLM owns semantic interpretation and structured extraction. Application code owns execution.**

LLM is responsible for:
- understanding user requirements → entity + fields
- generating bounded WorkflowPlan (3–5 search queries, max_results, validation rules, dedupe keys)
- structured extraction from clean Markdown + schema → records + evidence quotes
- later (explicitly approved only): source classification, ambiguity/record/entity resolution, relevance scoring, conflict interpretation — as typed inputs to deterministic code, never as control flow

LLM is NOT responsible for:
- database state, workflow state / transitions, retry logic, progress tracking
- type validation, normalization, date/numeric parsing, exact/fuzzy dedupe decisions
- arbitrary code execution, tool loops, storage, export

Deterministic code owns everything in the NOT list.

## 7. Permitted-sources-only policy

Operate within permitted boundaries. In scope: public pages, public APIs, public
search results, public job/company/news/docs pages, RSS/feeds. Out of scope:
authenticated/private pages, paywalled content, credential collection, private user data.
Be robots-aware, rate-limited, with timeouts, retries, domain allowlists.
See `09-CRAWLING-POLICY.md`, `20-SECURITY-AND-SAFETY.md`.

## 8. Quality bars worth overengineering

1. WorkflowPlan (clean, strongly typed) 2. Provenance (every cell traceable)
3. Execution events (convincing live run) 4. Provider abstraction (replaceable search/LLM/crawl).

## 9. One-line architecture (for presentation)

"An AI planning layer converts natural-language data requirements into a structured
collection plan, a controlled execution engine gathers permitted web data,
deterministic validation and deduplication turn it into a clean dataset, and every
field remains traceable to its source."

## 10. Agent rules

- Smallest implementation satisfying the documented contract.
- Do not replace deterministic logic with an LLM.
- Do not add infrastructure because it is common in production.
- Do not introduce a framework/library unless explicitly permitted or required by a doc.
- If genuinely unspecified and consequential, flag ambiguity before deciding.
