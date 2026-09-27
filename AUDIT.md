# DATAGOBLIN audit — dead code, dummy fallbacks, structure (mark-only pass)

Date: 2026-09-26. Rule for this pass: **NOTHING deleted.** Every candidate
below is marked `[DELETE?]` / `[FIX?]` / `[KEEP]` with file:line evidence.
Deletions, moves, and creations happen only on the user's green flag.

Verification baseline (all observed, this pass):
- `pytest backend/tests/`: **143 passed**
- `py_compile`: 67 files OK
- `e2e_file_run.py`: clean end-to-end
- `db_verify.py`: 9/9 tables, RLS 9/9, 9 policies, full anon CRUD confirmed
- Repo surface parity `LocalRepo` vs `SupabaseRepo`: identical public methods
- Fixed during this pass (not deletions, required for stated goals):
  `pip install supabase` — requirements listed it but it was never installed,
  so the Supabase flip would have crashed on first use. Now importable.

## A. Marker sweep — CLEAN
No `TODO / FIXME / XXX / HACK / NotImplemented / lorem / foobar` in
`backend/app`. All `stub|mock|dummy|placeholder|hardcod` hits are comments
asserting NOT-stub status, or documented `try/except: pass` fallbacks.
No prose mocks, no fabricated data paths anywhere in the pipeline.

## B. DELETE candidates (awaiting green flag)

- `[DELETE?] D1 backend/app/events/stream.py:48 EventBus.format_sse` — zero
  callers (`main.py` formats SSE inline for `EventSourceResponse`). Dead helper.
- `[DELETE?] D2 backend/app/services/provenance.py:9 field()` constructor —
  zero callers (`validator.wrap_record` builds source dicts inline). The
  `summarize()` in the same file is live — delete the function, keep the file.
- `[DELETE?] D3 backend/app/schemas/base.py` (whole file) — the four Protocols
  (`SearchProvider/PageFetcher/LLMProvider/DedupEngine`) have zero imports;
  the codebase uses duck-typed callables instead. The docs/05 "swappable seam"
  was never adopted. Either adopt it or delete the file.
- `[DELETE?] D4 backend/app/providers/agentic/` — EMPTY directory. No files,
  no imports. (Phase-6 verdict was SKIP mini-agent, so nothing is planned
  for it. If a future agent stage ever lands, recreate then.)
- `[DELETE?] D5 backend/app/providers/crawl/docling.py` — zero callers,
  `FEATURE_DOCLING` off, crawler routes `document:docling` to http/crawl4ai.
  Dormant Later hook; 9 lines. Delete or keep as the Phase-1.5 stub — call it.
- `[DELETE?] D6 backend/requirements.txt:25 typesafe-sdk` — zero imports
  anywhere (`pip show`: not installed either). Jev runs on raw OpenRouter
  REST. Remove the line. Related: `skills/jev/SKILL.md:3` still documents
  `pip install typesafe-sdk` + typesafe.ai docs — refresh that doc on flag.
- `[DELETE?] D7 backend/app/repositories/supabase_repo.py:65` — duplicate
  `return self._wrap("finalize_dataset", _tx)` directly after line 64's
  identical return. Unreachable line. (One-line fix, zero behavior change.)
- `[DELETE?] D8 backend/app/providers/llm/generate.py:101
  `_ = inner_schema`` — no-op statement. Harmless lint noise; remove with D7.
- `[DELETE?] D9 backend/app/services/planner.py:49+54` — `entity = max(...)`
  computed twice; line 54 overwrites line 49's identical result. Delete one.
- `[DELETE?] D10 backend/app/services/planner.py:85-93 _fallback_plan` —
  the only HARDCODED-data fallback in the codebase (fixed company plan).
  Fires only if the total rule-based compiler raises (practically never),
  honestly labeled `provider=fallback`. Recommendation on flag: delete it and
  let `compile_plan` raise `E_VALIDATION` instead of serving canned content.
- `[DELETE?] D11 backend/scripts/diag_extract.py` — prior-session single-URL
  diagnostic, superseded by `bench_baseline.py` + `gen_selectors.py`.
- `[DELETE?] D12 fixtures/` (whole tree: `startup-dataset.json`,
  `startup-a.html`, `malformed.html`, 3 empty dirs) — zero code references;
  `inputs/` superseded it as the live corpus.
- `[DELETE?] D13 prompts/*.md` (5 files) — zero code references. All prompts
  are inline f-strings (the tested truth). These docs WILL drift; they already
  duplicate `build_prompt`/planner wording. Delete, or formally adopt as the
  prompt source of truth (would require a loader + tests).
- `[DELETE?] D14 Unused settings in backend/app/core/config.py` (defined,
  never read outside config; each verified by import sweep):
  `VITE_API_URL:19` (frontend env concern, not backend), `DB_POOL_MIN:26`,
  `DB_POOL_MAX:27`, `DB_STATEMENT_TIMEOUT_MS:28` (supabase-py manages its own
  connections; local adapter uses files), `EXPORT_CSV_ENCODING:90`
  (exporter never reads it), `LOG_JSON:111` (logging is always JSON),
  `CORRELATION_HEADER:115` (`api/deps.py` hardcodes the header name),
  `FEATURE_MCP:102`, `FEATURE_BROWSER:101` (no code paths),
  `QDRANT_URL/API_KEY/COLLECTION:105-107`, `LANGSMITH_API_KEY/TRACING/PROJECT:
  112-114` (no tracing calls), `API_WORKERS:17` (README run command uses
  bare `uvicorn`, never this value).
  `FEATURE_DOCLING/JINA/SEMANTIC_DEDUP` are live gates — KEEP.

## C. Stale docs/contracts (correct on flag, not delete)
- `[FIX?] C1 backend/app/main.py:1` — docstring says "11 endpoints"; there
  are 14 (added `/api/jobs/{id}`, `/api/map`).
- `[FIX?] C2 contracts/api.openapi.yaml` — predates Phase 5: no `/api/jobs`,
  no `/api/map`, no `jsonl`, no `credit_budget`, no run-level `seed_urls`.
- `[FIX?] C3 docs/34-LOADED-SKILLS.md:27`,
  `docs/ARCHITECTURE-DETAILED.md:12` — say "Gemini primary"; production order
  is Groq primary → Gemini fallback since the quota incident.
- `[FIX?] C4 contracts/workflow-plan.schema.json` — missing the runtime
  `credit_budget` key (harmless: extras pass through, but mirror it).

## D. Review notes (no action without a decision)
- `[NOTE] N1 JEV_MODEL`: `.env` sets `JEV_MODEL=jev-latest`, config default is
  `~typesafe/jev-latest`. Live probes succeed with the `.env` value. Confirm
  which is canonical; remove the other.
- `[NOTE] N2 Auth surface`: reads (`/api/history`, `/api/datasets/*`,
  `/api/jobs/*`) are open; writes require `API_KEY` when set. Intentional
  local-dev posture — confirm before any exposure.
- `[NOTE] N3 langchain seam`: `langchain_client.py` is live (supervisor
  fallback chain) and the `langchain` meta-package IS installed — but only
  transitively. Add an explicit `langchain>=1.0` pin to requirements so a
  future resolver upgrade can't silently amputate the supervisor's LLM path
  (failure mode: silent fallback to deterministic break, no error).
- `[NOTE] N4 Manual supervisor path (`agents/graph.py:167-176`) only executes
  when langgraph is absent; it is installed, so that branch is untested in CI.
  Keep as fallback; consider one forced-ImportError test on flag.
- `[NOTE] N5 Frontend (`frontend/src`, 8 files) is fully connected, no
  orphans — but has no UI for `/api/jobs`, `/api/map`, budgets, or selectors.
  That is Phase-7 scope, not dead code.
- `[NOTE] N6 skills/ (25 repo guidance docs) vs .agents/skills/ (installed:
  supabase ×2) vs .claude/skills/ (installer symlinks)` — three different
  things sharing a name; all intentional, do not "dedupe".
- `[NOTE] N7 research/` contains only its README (empty inbox by design);
  `design/` holds frontend tokens/screenshots (propose moving under
  `frontend/` on flag, not deleting).

## E. Proposed structure (execute on green flag only)
1. Delete D1–D14 per decisions above (D5/D13/D14 need your explicit call).
2. Apply C1–C4 doc/contract corrections.
3. Add `.gitignore` (contents below) + `.env.example` (mirror of `.env` with
   secrets blanked — docs/CONSISTENCY-REPORT already assumes it exists).
4. Move `design/*` → `frontend/design/`; keep `research/` as the inbox.
5. Keep: `backend/{app,tests,scripts,migrations,selectors}`,
   `inputs/` (live corpus), `outputs/` (runtime artifacts, ignored),
   `contracts/`, `docs/`, `skills/`, `fixtures/` only if flag says keep.

Proposed `.gitignore`:
```
.env
__pycache__/
.pytest_cache/
.datagoblin-local/
outputs/
node_modules/
*.sqlite3
*.bin
```

## F. Explicitly NOT flagged (live, verified)
Runner → crawler → fetcher → extractor → validator → deduper → exporter
chain; Jev 4 families + deterministic policies; Tavily/Gemini/Groq providers;
impersonation rung (allowlist empty = inert); politeness/stats; selectors +
`quotes.toscrape.com.json`; metering/ledger/jobs/map endpoints; migrations
001/002; db_* scripts; bench script; all 143 tests; `jina_fetch`,
`langchain_client`, `file_fetch` (gated/dormant-by-design adapters with live
dispatch paths, not dead code).

---

# ADDENDUM — corrections, measured 2026-09-27

This audit was accurate about the repo it inspected. Several of its conclusions
are now **wrong**, because the code they described has been fixed or replaced.
Corrections first, so nobody acts on a stale verdict.

## R1 — `jina_fetch` was called "live … not dead code" (was: line 141-143)

**Wrong.** `jina_fetch` is complete and gated, but `"jina"` is absent from the
crawl order table in `services/crawler.py`, and `method` is only ever drawn from
that table. No dispatch can select it. The `JINA_API_KEY` in `.env` is unused.
The audit counted the presence of a `if method == "jina"` branch as a live path;
the branch is unreachable.

## R2 — `FEATURE_DOCLING/JINA/SEMANTIC_DEDUP` are "live gates - KEEP" (line 76)

**Half wrong.**
- `FEATURE_JINA` gates a real function that nothing can reach (R1).
- `FEATURE_DOCLING` is read by **nothing** in executable code. Its only mention
  is a comment string in the order table. `docling.py` has zero callers, so
  PDF/JSON sources are **always skipped**.
- `FEATURE_SEMANTIC_DEDUP` is not a gate but a **kill-switch**: setting it
  `true` raises on purpose, before any merge work.

## R3 — dead budget settings were not listed (line 73 area)

Three settings documented in `docs/09-CRAWLING-POLICY.md` as hard limits have
**zero readers** anywhere in `app/`:

- `RUN_MAX_REQUESTS`
- `RUN_MAX_PAGES_PER_SOURCE`
- `RUN_MAX_SEARCH_QUERIES` (the live budget is `SUPERVISOR_MAX_QUERIES`)

`docs/09` has been corrected; see its "Hard limits — corrected" table.

## R4 — `DATABASE_URL` was dead; the app wrote JSONL (undocumented)

The audit flagged `QDRANT_*`, `DB_POOL_*`, `LANGSMITH_*` as unused but **missed
the largest one**: `DATABASE_URL` was never read by `app/` at all. With
`SUPABASE_URL`/`SUPABASE_KEY` empty, `build_repo()` silently selected
`LocalRepo`, so the app wrote 379KB of JSONL while a fully migrated, empty
Postgres sat unused with nothing reporting it.

`SupabaseRepo` has since been **deleted** and replaced by
`PostgresRepo` (psycopg against `DATABASE_URL`). `PERSISTENCE` is now explicit
and reported by `GET /api/health`; the silent fallback is gone.

## R5 — `RAG/vector` status was correct and is still correct

`QDRANT_*` remain read by nothing. There is no embedding provider. Semantic
dedup is an explicit fail-fast stub. This audit was right.

## R6 — the "143 tests" line

The suite is now 361 tests, including regression coverage for every defect
above. The count itself is not interesting; the point is that the three worst
bugs had the same shape — **a check that could not fail was reported as a
pass** — and the new tests assert the *absence* of false claims (no event type
emitted but undeclared, no "verified" without a verdict, no declared type
nothing sends).

## Still open from this audit

- `.env` is committed-readable with live secrets; `.gitignore` is proposed, not
  present. **Largest outstanding risk.**
- No frontend browser tests.
- `run_events` has no `type` column, so the persisted audit trail cannot
  distinguish event kinds after the fact.
- `REDUCING` / `FINALIZING` stages are declared in the enum and the transition
  table but never emitted.
