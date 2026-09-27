# 37 — CURRENT STATE: is the app end to end?

> **Start here.** What actually works today, what was measured, and what is
> still broken. Written 2026-09-27 after the evidence-store work; the other docs
> in this folder describe intent, this one describes reality.

## Doc map for the changed areas

| if you care about | read |
|---|---|
| does it work end to end | **this file** |
| where the page content is kept, and why | `29-EVIDENCE-LAYER.md` |
| what a "verified" field means | `11-VALIDATION-SPEC.md` |
| what a record cites | `12-PROVENANCE-SPEC.md` |
| the shape of the stored evidence | `14-DATABASE-SCHEMA.md` |
| what the frontend receives | `16-SSE-EVENT-CONTRACT.md` |
| what each judge is for | `32-JEV-USAGE.md` |
| crawl order, escalation, real limits | `09-CRAWLING-POLICY.md` |
| the path a run takes | `06-DATA-FLOW.md` |
| concurrency and per-chunk failure | `10-EXTRACTION-SPEC.md` |
| what is stubbed vs live | `13-DEDUPLICATION-SPEC.md` |
| honesty rules on failure | `19-ERROR-HANDLING.md` |
| the supervisor graph | `31-AGENT-STATE-POLICIES.md` |
| keys, models, measured latencies | `23-ENVIRONMENT.md` |
| known-bad audit findings, corrected | `../AUDIT.md` (addendum) |

---


Answered with a measured run, not an inspection. Live run, real Supabase
Postgres, real Tavily, real Crawl4AI, real OpenCode, real free-tier models.

## Verified working, end to end

**Research run** — one prompt to a stored, cited dataset:

| check | result |
|---|---|
| run status | `COMPLETED`, `partial=false` |
| records stored | 112 (dataset row count matches rows written) |
| pages stored with content | 9 (~950KB markdown + raw HTML) |
| verified fields | 226 |
| quote resolves **exactly** at its cited offsets in the stored page | **226 / 226** |
| failed to resolve | **0** |
| verified fields missing a `page_id` | **0** |

**Fan-out (`/api/intel/ask`)** — 10 free text models asked one question: 8/10
answered, all **8 distinct models** (`distinct_models: 8`, `independent: true`).
The 2 failures are genuine upstream `UnknownError` 500s on `mimo-v2.5-free` and
`muse-spark-1.2-contributor-free`, retried and still failing.

**Suites**: 361 backend tests pass. Frontend builds. `git diff --check` clean.

## What "end to end" does NOT yet mean

Stated plainly, because "it works" was the claim that hid the most damage:

1. **`run_events` has no `type` column.** The event type lives only in the SSE
   payload, so the persisted audit trail cannot distinguish `record.rejected`
   from `run.completed` after the fact. Needs a migration. (docs/16, docs/14)
2. **One known flaky test.** `test_markdown_report_renders_evidence` failed once
   in ~6 full-suite runs. Never reproduced in isolation or in three subsequent
   full runs; cause not established. Not fixed, only observed.
3. **PDF and JSON sources are always skipped.** `document:docling` and
   `structured:api` resolve to methods that hit the non-HTML guard; Docling has
   zero callers. (docs/09)
4. **Jina is unreachable.** Implemented, gated, but absent from the crawl order
   table, so no dispatch can select it. (docs/09)
5. **No cross-run dedup**, and `dataset_records` has no unique index. (docs/13)
6. **The dedupe-key fallback is weak** — an empty `dedupe_keys` degrades to
   "every field name sorted", and the planner often emits none. (docs/13)
7. **`REDUCING` / `FINALIZING` stages are never emitted**, though the enum and
   transition table publish them. (docs/16)
8. **Dead config that reads like a feature:** `QDRANT_*`,
   `FEATURE_BROWSER`, `FEATURE_MCP`, `FEATURE_DOCLING`, `RUN_MAX_REQUESTS`,
   `RUN_MAX_PAGES_PER_SOURCE`, `RUN_MAX_SEARCH_QUERIES`, `DB_POOL_*`.
9. **The frontend has no automated browser tests.** It builds and a human
   confirmed `/intel` renders; a UI change is not covered by the 361 tests.
10. **`.env` is committed-readable with live secrets** (Supabase password,
    Tavily, Gemini, Groq, OpenRouter, Jina, Zen). `.gitignore` is proposed, not
    present. Out of scope here, but it is the largest outstanding risk.

## Performance note

The pipeline was LLM-latency bound, and the configured model was the reason.
Measured on a realistic 21.5k-character prompt:

| model | latency |
|---|---|
| **big-pickle** | **4.9s** |
| muse-spark-1.3-contributor-free *(was configured)* | 29.2s |
| nemotron-3.5-lightning-free | 162.1s |
| mimo-v2.6-flash-free | 170.2s (timed out) |

`OPENCODE_MODEL` is now `opencode/big-pickle` (backup: `.env.bak-model-161920`).
**This is a config decision, not a code fix — revert that one line to undo it,
and re-measure before concluding anything about the others.** Changing it traded
extraction latency for validation volume (728 records on one run), which is why
the judge budget exists.

Three concurrency fixes did the rest, all in code, all measured:

- extraction **chunks** run concurrently, not sequentially (the sum of 4 calls
  exceeded the per-page timeout, so every page returned 0 records)
- extraction **pages** run concurrently, bounded at 3, with rate-limit pacing
  kept and applied in waves
- Jev-C conflict adjudication runs concurrently

## The pattern behind the worst bugs

Three separate defects had the same shape: **a check that could not fail was
reported as a pass.**

- a missing judge returned `SUPPORTED`, and the whole test suite passed on it
- `"partial kept"` was emitted while every record was discarded
- `research_continuation` was documented as a coverage judge and was a no-op

Each had a comment or a docstring asserting the opposite of the behaviour. The
defences added are all of the same kind: assert the *absence* of a claim
(no type emitted but undeclared, no verdict without a judge, no "verified"
without a ruling), because asserting the presence of a feature is what let
these through.
