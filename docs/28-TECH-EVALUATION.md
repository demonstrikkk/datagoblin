# 28 — TECHNOLOGY EVALUATION MATRIX (lean freeze: MVP = 12, no soup)

Story: **GENERATE (Gemini/Groq) → ORCHESTRATE (LangGraph) → JUDGE (Jev) → EXECUTE (Runner) → PROVE (evidence).**

## MVP (12 — freeze, build these)

| Technology | Role | Verdict |
|---|---|---|
| FastAPI | API / control plane | ✅ Keep |
| Supabase | persistent truth | ✅ Keep |
| Gemini | planning / extraction (generate) | ✅ Keep |
| Groq | fallback | ✅ Keep |
| Instructor + Pydantic | typed LLM outputs | ✅ Keep |
| Tavily | discovery | ✅ Keep |
| HTTPX | cheap fetching | ✅ Keep |
| Trafilatura (+BS4) | content reduction | ✅ Keep |
| Crawl4AI | JS-heavy fallback | ✅ Keep |
| LangGraph | bounded research orchestration (moves state only) | ✅ Keep |
| Jev | typed decision layer — 4 families (CORE differentiator) | ✅ CORE |
| RapidFuzz | dedupe L1/L2 (STOP here for MVP) | ✅ Keep |

## Later (adapter/interface only — do not build now)

| Technology | Role | Phase |
|---|---|---|
| Docling | documents | 1.5 (only if demo needs PDFs) |
| Jina Reader | difficult-page fallback | 1.5 |
| FastEmbed / Qdrant / LanceDB | semantic dedupe L3 | 2 (evaluate) |
| DuckDB / Polars | analytics/transform (MVP: Supabase→CSV/JSON) | 2 (evaluate) |
| PydanticAI | orchestration alternative | 2 (evaluate, no switch) |
| MCP | external tool expose | 3 |
| Stagehand / Browser Use | browser last resort | 3 |
| Redis / workers / Temporal / K8s / ClickHouse | scale (deferred, not banned) | scale-gated |

Rejected: smolagents / Agno (wrong direction — autonomous loops replace Runner); Firecrawl self-host (ops weight); stealth/proxy/TLS-spoof/CAPTCHA-bypass (never — permitted sources only).
