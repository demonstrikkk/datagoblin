# DATAGOBLIN — Architecture of Record

> **Status:** verified against the code on 2026-09-30.
> **Purpose:** a single file that describes what this system *actually is*, so no
> reader has to trust the other 47 documents, or a docstring, or a comment.
> **Method:** every claim carries a `file:line`. Anything I could not verify is
> marked **[unverified]** and is not asserted.
>
> **This file supersedes `docs/00`–`docs/36` and `AUDIT.md` for factual purposes.**
> Those files are retained as a record of intent, and several of their claims are
> false; §10 lists every correction.

---

## 1. What this is

A single-operator web application that takes a plain-language request, searches the
open web, extracts structured records from real pages, and stores a dataset where
each value carries a quote that points back into text the system actually saved.

**Five things it is not.** Each of these is stated in the project's own docs, and
each is confirmed by the code — this is the honest shape, not a disparagement:

| Not this | Evidence |
|---|---|
| Not multi-user | No `user_id`, `tenant` or `role` column exists in any of the 6 migrations. `dependencies.py` gates one shared API key. |
| Not a vector store | `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION` (`config.py:216-218`) have zero readers. `deduper.py:24-27` raises deliberately when `FEATURE_SEMANTIC_DEDUP` is on. `qdrant` is commented out of `requirements.txt:32`. |
| Not continuously tested | No `.github/`, no workflow file, no `Makefile`, no `pyproject.toml`. The 9 e2e suites are `main()` scripts, invisible to `pytest`. |
| Not an "agentic" system in the marketing sense | Five deterministic stages. Two LLM call sites (plan compile, extract) plus one judge. Every routing decision is arithmetic. |
| Not honest in its own README | The headline evidence claim is false on two reachable paths (§5). |

**Real counts** (measured, not copied from docs):

| Thing | Actual | Docs claim |
|---|---|---|
| HTTP routes | **35** | 11, 13, 14, 16 |
| Backend tests | **762 passing** | 143, 361, 389 |
| e2e suites | **9** | 4, 5 |
| SSE event types | **14** | 13 |
| Migrations | **6** | 3 listed in README layout |
| SSE event types wired to the browser | **13** (one missing) | 13 |

---

## 2. Configuration that is actually in force

`backend/app/core/config.py` reads `.env` by **absolute path**, resolved by
walking up from the file (`config.py:14-21`). This was a real bug: the previous
`env_file=".env"` was relative to the *process working directory*, so starting
the API anywhere but the repo root silently emptied `OPENCODE_PASSWORD`, the
client omitted its `Authorization` header, and every extraction returned 401 —
the same symptom as a wrong password, requiring the opposite fix.

Live values that change behaviour:

| Setting | Value | Effect |
|---|---|---|
| `PERSISTENCE` | `postgres` | Remote Supabase Postgres via `psycopg`, **not** a pooler-agnostic local store |
| `RUN_MAX_RUNTIME_S` | **600** | A run is cut off at 10 minutes. The README says 1200. |
| `RUN_MAX_PAGES` | 12 | Hard cap on pages opened per run |
| `RUN_MAX_DEPTH` | 2 | Seed + one hop + one more |
| `OPENCODE_STRICT` | `true` | The LangChain cloud rung never fires |
| `ALLOW_UNAUTHENTICATED` | `true` | **The API is open.** `API_KEY` is empty. |
| `FEATURE_SEMANTIC_DEDUP` | `false` | Fuzzy tier is string similarity only |
| `RUN_MAX_JUDGE_CALLS` | 400 | Per-run judge budget |

> **Security note, stated plainly:** this deployment is an unauthenticated
> metered proxy holding live Tavily / Gemini / Groq / OpenRouter / Zen keys. The
> OpenAPI contract states the opposite ("fails closed"). This must be fixed before
> any deployment beyond localhost.

The database is reached through **PgBouncer in transaction mode**, so
`psycopg_pool` is constructed with `prepare_threshold=None`; without it,
`DuplicatePreparedStatement` surfaces as intermittent 503s from `get_page`.

---

## 3. The pipeline, as executed

Five stages are emitted. Two more are *declared and never emitted* (§3.6).

```
plan compile (LLM)  →  DISCOVERING  →  FETCHING  →  EXTRACTING
                                                     ↓
                        COMPLETED / PARTIAL / FAILED  ←  VALIDATING → DEDUPLICATING
```

### 3.1 Plan compile — `POST /api/workflows/compile`

A model turns a sentence into a `WorkflowPlan` (`schemas/plan.py:22-36`): `goal`,
`entity`, `fields: list[FieldSpec]`, `search_queries` (1–5). Field names are
constrained to `^[a-z0-9_]+$`. This is the first of exactly two LLM call sites
in the steady-state path.

The compiled plan is then refined conversationally (`/api/workflows/refine`).

### 3.2 DISCOVERING — `services/discovery.py` + `agents/graph.py`

`run_supervisor` compiles a **three-node** graph — `search → screen → decide` —
under LangGraph with an `InMemorySaver` keyed by `run_id` (`graph.py:455-496`).
Because `langgraph` imports cleanly in this environment, **the manual fallback
loop is dead code here**; it is retained only for environments without LangGraph.

**`search_node`** (`graph.py:84-115`): up to 3 pending queries per round, 8 total
(`SUPERVISOR_MAX_QUERIES`), 5 results per query, blocked hosts passed as
`-site:` exclusions. A query that throws is *not* marked tried, so only
`max_iterations` bounds retries.

**`screen_node`** (`graph.py:239-334`) — the order matters:

1. **Robots gate first**, sequentially, before any judge call. A candidate we may
   not crawl is dead on arrival; one live run accepted two `robots-disallowed`
   sources and spent its whole budget.
2. **`_plan_relevance` sort, before the cap** (`graph.py:302`). This is a
   correctness fix, detailed in §8.2.
3. Jev-A source screening in batches of 5, until `max_pages` accepted sources.
4. `judgment == "NO"` → skipped silently.

Seeded URLs bypass screening entirely by construction — they enter
`accepted_sources` directly (`discovery.py:103-113`).

**`decide_node`** (`graph.py:392-438`): Jev-D decides whether coverage is
sufficient; otherwise a strategy model picks `REFINE_SEARCH` / `FETCH` /
`FINALIZE`. `next_queries` is capped to 3 by `decisions.py:14-22`, and the whole
search budget is capped at 8, after which the decision degrades to `FETCH` with
`" [budget-capped]"`.

**Loop termination** is bounded by three independent limits: `max_iterations=3`,
`max_queries=8`, and the `max_pages` cap. With `max_pages=12` and 1–5 queries,
**one search→screen round is the normal case.** Refinement happens only when a
round accepts nothing.

### 3.3 FETCHING — `services/crawler.py`

`fetch_all` runs 4 workers against a budget of 12, with a per-domain cap of 3, a
per-parent child cap of 8, and `max_depth` 2.

**The fetch waterfall** (`_ORDER`, `crawler.py:40-43`) is keyed on the triage
route:

| Route | Rungs actually used |
|---|---|
| `web:http`, `web:crawl4ai` | `http`, then `crawl4ai` |
| `document:docling` | `http`, then `crawl4ai` — **docling is never in any list**; a PDF is fetched over HTTP and reduced as HTML |
| `structured:api` | `http` only |
| `impersonate` | inserted after `http` **only if the host is allowlisted**; `IMPERSONO_ALLOWLIST` is unset, so it never fires |
| `jina` | present in the fetch dispatcher but in **no** `_ORDER` entry — unreachable from a run |

Escalation: if a page looks like a JS shell, hollow, or sparse, the crawler tries
the next rung and keeps the best by reduced-text length.

**Robots** (`fetcher.py:155-186`): per-host cache, 1 h TTL. `401`/`403` on
`robots.txt` → disallow all. `>= 400` or an exception → **allow** (standard
practice). A block is a normal outcome: counted as `skipped`, persisted with
`status: "skipped"`, never a failure.

**Politeness**: per-host lock, `AutoThrottle`-style EMA that only ever *increases*
on trouble, `Retry-After` honoured, capped at 300 s. 2 MB / 100 kB byte caps.

**Link ranking** (`score_link`, `_rank_children`, `canonical_url`): see §8.2.

**Page persistence** is the pivot of the whole design — `pages.markdown` is
written as `reducer.page_evidence_text(page)`, the *same function* the extractor
will later read (`crawler.py:667`). That is what makes quote offsets resolve.

### 3.4 EXTRACTING — `services/extractor.py`

1. **Deterministic first** (`extractor.py:220-241`): learned selectors and regex
   patterns, zero LLM cost. If they return records *and* full coverage, the
   pipeline stops there and reports `selectors:<domain>`.
2. Otherwise the page is reduced to evidence text and chunked
   (6000 chars, 600 overlap, ≤ 4 chunks, 12 000 char limit) and the chunks are
   run **concurrently** — wall cost is the slowest chunk, not the sum.
3. A **bounded regen**: one extra call, only if nothing was extracted *and* a
   coverage tripwire says the page does contain the requested terms.
4. `coerce_output` validates each record independently (one malformed record
   never kills the batch), drops placeholder values, and narrows evidence to
   surviving fields.

### 3.5 VALIDATING and DEDUPLICATING

`verify_field` runs cheap deterministic gates first, the judge last:

1. value present? 2. type matches? 3. **is the quote a substring of the stored
page text?** 4. reference known? 5. **judge**.

Jev-C then adjudicates conflicting cells concurrently during DEDUPLICATING, after
the canonical set is published so a hard timeout still keeps it.

### 3.6 Stages that are declared but never emitted

`constants.py:9,13` define `REDUCING` and `FINALIZING`, and `LEGAL_TRANSITIONS`
routes through them — but **neither is referenced anywhere outside
`constants.py`**, and `LEGAL_TRANSITIONS` is never imported. The runner docstring
also advertises a `NORMALIZE` stage, which does not exist as a `RunStage`;
normalization runs inside the item pipeline and emits nothing.

The UI states this honestly — *"Reduce and Finalize are declared stages the runner
does not emit"* — and an e2e test asserts that honesty. This is correct and should
not be "fixed" by emitting them: they represent work that genuinely happens
inline, and a node in the timeline that was never entered would be a fiction.

---

## 4. What the model actually does

This is the section most worth reading, because the project's own docs overstate
the role of the LLM.

| Decision | Made by | Cost |
|---|---|---|
| Sentence → schema, entities, queries | **LLM** | 1 call |
| Refine the plan | **LLM** | 1 call |
| Next search queries | **LLM** (strategy) | ≤ 3 per round |
| Continue or stop searching | **LLM** (Jev-D) | 1 per round |
| Is this source worth fetching | **LLM** (Jev-A) | 1 per candidate |
| Records from a page | **LLM** | 1 per chunk |
| Does this quote support this value | **judge** — but see below | budgeted |
| Which rival wins a conflict | **judge** (Jev-C) | budgeted |
| Page budget, depth, domain caps | **arithmetic** | free |
| Link ranking | **arithmetic** | free |
| Identity matching, dedupe tiers | **arithmetic** | free |
| Coverage, yield, profiling | **arithmetic** | free |

**The judge short-circuits more often than it calls anything.**
`jev.py:148-156`: if the value is a substring of its own quote — the normal case,
since the extractor lifts values out of quotes — the verdict is
`SUPPORTED`, `confidence 0.95`, `provider: "deterministic"`, with **no model
call at all**. This is a deliberate, well-reasoned cost decision (a live run
reached 728 extracted records; asking a model about every field of each meant
thousands of round trips).

The consequence is that most `verified` values are verified *by arithmetic*, not
by a model. That is a defensible design and the code says so in a docstring. **No
user-facing document says it.**

---

## 5. The evidence contract, stated accurately

The README's headline is:

> "every non-null field cites the exact stored text it came from — or the field
> stays null."

**That is false on two reachable paths.** Both are verified.

### 5.1 Offsets exist only for `verified` cells

```python
# validator.py:210
start, end = locate_quote(quote, source_text) if st == "verified" else (None, None)
```

`judgment_unavailable` and `rate_limited` **both keep the value**
(`validator.py:166,177,187`). So a non-null value routinely ships with
`start: null, end: null`. It carries a quote *string*, but the offsets that would
prove the quote re-locates in the stored page are absent.

### 5.2 A value can be marked `verified` without verification

```python
# deduper.py:119-123 — Jev-C adopting rival B
cell["value"] = rival.get("value")
cell["source"] = rival.get("source", cell.get("source", {}))
cell["verification_status"] = "verified"
```

No substring gate, no judge, no re-location. The rival's offsets are whatever
that rival had — frequently `None`. `verified` here means "a decision was made",
not "evidence supports this".

### 5.3 What is genuinely true

- `page_evidence_text()` is the single definition of evidence text, called by the
  store, the extractor, backfill and refresh. Verified.
- `test_provenance_page_link.py:100-102` really does slice
  `stored[url]["markdown"][start:end]` and assert it equals the quote. This is a
  real round-trip test, and the README describes it accurately.
- The value→quote→stored-page chain is intact for every `verified` cell with
  non-null offsets.

---

## 6. Data model

Six migrations. Tables that exist and are written:

| Table | Purpose | Notes |
|---|---|---|
| `workflows` | compiled plans | upserted on `id` |
| `runs` | run record | `status`, `partial`, `error`, `stats` |
| `run_events` | audit trail | `type` added by migration 006 |
| `sources` | per-run fetch outcomes | `reused_from_run_id`, `reused_page_id`, `retrieved_at` |
| `pages` | stored evidence | `markdown` = the evidence text; `raw_html` snapshot |
| `datasets`, `dataset_records` | output | records in `row_json` |
| `exports` | export ledger | `(id, dataset_id, format, byte_size)` — **no payload column** |

`pages` has a unique index on `(run_id, url)`, so re-fetching updates the evidence
rather than forking it. `dataset_records` has **no** unique index and no
`ON CONFLICT`: the write is a bare `COPY`, so cross-run duplication is possible
and is not prevented by the schema.

**Migration 006's backfill is largely inert.** It pattern-matches message text to
infer `type`, but the actual messages are `Extracted N from …`, `Merged N
duplicates`, `Record dropped at …`. Almost every historical event ends up with
`type: ''` — honestly unidentified, which the migration's own comment accepts as
the safe outcome.

---

## 7. Verified defects

Six, each reproduced against the code. Ordered by blast radius.

### D1 — Stored-page reuse does not run. **Cost impact.**

`main.py:362` calls `politeness_svc.fingerprint("GET", url)` inside `_reuse`, but
there is **no module-level import** binding that name. It is imported only
*locally*, inside `intel_ask`, at `main.py:1261`. Verified by AST scan: module-level
bindings of `politeness_svc` = **none**.

So `_reuse` raises `NameError`; `crawler.fetch_all:564-571` catches it and falls
through to a real fetch. **Every run re-downloads every URL it already has.**
This matches the observed UI: *"Reused, not re-fetched: 0 — nothing was
reused."* The reuse tests pass because they inject their own closure, so the
HTTP path is never exercised.

### D2 — `source.reused` never reaches the browser. **UI completeness.**

The server names every SSE frame `{"event": <type>}` (`main.py`, the `_gen` in the
stream route). `useRunStream.js:271` registers named listeners for **13** types —
`source.reused` is not among them, although `useRunStream.js:66` has a `case
'source.reused':` branch. `EventSource` routes only untyped frames to
`onmessage`, so that branch is unreachable. Any fix to D1 without this is
invisible in the product.

### D3 — Run counters are always zero. **Reporting.**

`_emit` builds `records_fully_verified`, `records_needing_review`,
`fields_verified`, `fields_judgment_unavailable`, `fields_rate_limited`.
`schemas/run.py:Counters` declares `verified` and `needs_review`. Pydantic drops
the unmatched keys, so `GET /api/runs/{id}` always reports `verified: 0,
needs_review: 0`.

### D4 — `partial` and `no_yield_reason` are computed then discarded.

Both are set on the in-memory `RUNS` entry with explanatory comments. `RunView`
has no such fields, so no endpoint can report them. The run page *does* show
honest partial messaging, because it reads the dataset — but the run API cannot.

### D5 — The judge budget is spent on calls that never happen. **Correctness.**

`validator.py:160` calls `budget.spend()` *before* invoking the judge, but
`jev.py:148` short-circuits deterministically for the common case. The 400-call
per-run cap is therefore consumed by free deductions, and genuine judgements can
be downgraded to `judgment_unavailable` — a value kept with no verdict, reported
as though a judgement was unavailable when it was never attempted.

### D6 — `/api/datasets/{id}/profile` is absent from the OpenAPI contract.

A real, tested endpoint (24 tests) that the dashboard and Coverage view depend
on. Every other registered route is documented; this one is not.

### Also true, lower severity
- `_explain_zero_yield` probes `page.get("text")` for near-empty pages, but the
  crawler never sets a `text` key, so that branch can only fire for rendered pages.
- `POST /api/datasets/{id}/columns`, `/conflicts/resolve` and
  `/api/selectors/save` all write immediately, contradicting the contract's
  "no route applies a change without an explicit `apply` flag".
- `GET /api/health` reports the *configured* adapter name; it issues no query, so
  it cannot report reachability, as the contract claims.

---

## 8. Work completed in this session

### 8.1 Punctuation is no longer stored as data

A dataset showed 15 fields where most cells contained an em dash or a curly
quote. Root cause, in two parts — the second was the real one:

1. The extractor prompt says to write `"NA"` when a value is absent. Models
   usually comply; when they do not, the usual substitute is punctuation, and
   each became a cell: present, unverified, displayed as a stray symbol.
2. **`DatasetPage.jsx` had been written through a cp1252 round trip.** The
   "no value" marker was the *mojibake of an em dash*. 26 other sequences in the
   same file were mangled the same way. The database was clean; **the UI was the
   thing rendering garbage.**

`backend/scripts/repair_placeholders.py` was written to prove point 1 against the
data. Run against this database it finds **0 cells** — which is exactly the point.

The invariant now enforced (`services/normalizer.py`): a value with no
alphanumeric character, or an explicit absence word, is not data. `0` and `False`
are values. A merely *unverified* value is kept — collapsing unverified into
absent would delete exactly the findings the statuses exist to distinguish.

### 8.2 Discovery ranks candidates instead of taking search order

Measured failure: *"find top 10 stocks in india share market"* opened 12 pages,
five of them `indiahandmade.com`, `desiclik.com`, `qalara.com`, `ishopindian.com`
and a `top-10-products-india-imports-from-the-usa` listicle — while the four pages
that could answer it competed for the same budget.

Candidates are now scored against the plan **before** the cap applies
(`graph.py:288-302`). Three things make it discriminate, and the third is the
subtlest:

- **the entity outweighs the goal** — "top 10" and "india" match a listicle about
  imports and an exchange's equity page equally; the word *stock* is in one only;
- **a bounded synonym list** — the plan says "stock", the pages say "live equity
  market" and "equities". Without this the entity term matched nothing and
  ranking collapsed back onto the goal words;
- **a plural rule**, because an entity of "stock" must meet a title of "Stocks".
  A single plural rule, not a stemmer: "market" and "marketing" are not variants
  of one another.

Editorial slugs (`blog`, `guide`, `review`, `top-`, `imports`, `reddit`) are
demoted, not dropped. Re-scoring that exact candidate set: the four financial
pages rank first, both blogs last.

### 8.3 Cost and safety hardening

- **`scripts/llm.ps1`** starts the LLM with its password read from the same
  `.env` the API reads. A run once returned zero records because the server had
  been started with the literal text `<copy OPENCODE_PASSWORD from .env>` — a
  placeholder from a comment, pasted verbatim. The server was happy with it.
- **`dev.ps1` now verifies the credential** and refuses to start anything else on
  failure, instead of reporting "up" for a server that answers 401 to everything.
- **Absolute `env_file`** (§2).
- **Run pre-flight** — a provider that cannot authenticate is refused in under a
  second rather than after twelve successful fetches.
- **`available()` no longer calls a 401 "reachable."**

### 8.4 Dataset operations

| Feature | What it does | What it refuses |
|---|---|---|
| **Top-up** | Fills empty cells in a *partially* filled column | Never overwrites a cell that holds a value |
| **Refresh** | Re-fetches sources and re-verifies their values | Requires a verified quote; keeps the old value as a reviewable **rival**; leaves unchanged values alone |
| **Add column** | Declares a field the schema lacks | Refuses to guess the name; separate route from the backfill that fills it |
| **Yield memory** | Derives which hosts actually produced values, by reading existing quotes | Reports zero for a host with no evidence, rather than guessing |
| **Dashboard** | One server-side aggregate | Cannot disagree with the dataset page — same coverage function |
| **Field profiler** | Per-field shape, trust and place | Omits charts the data can't support |

The refusals are the point. A profiler that draws a distribution for a field with
one distinct value is drawing a fiction; a refresh that overwrites silently
breaks the promise that a disputed value can be seen and judged.

### 8.5 Measured results

| Measurement | Before | After |
|---|---|---|
| Dashboard aggregate (15 datasets) | 31 s | **9 ms** warm, cached, invalidated on every write |
| Field profile (largest dataset) | — | 975 ms |
| Link candidates offered vs queued | 300 → 48 | same, now after relevance ranking |
| Stored pages that were nav/legal/root furniture | 28 of 151 (19%) | — |
| E2E race conditions | 3 fixed sleeps reading empty shells | content/stability waits |

---

## 9. Laya: evaluated, not integrated

**It exists, it is real, and it is a good fit.** [github.com/receptron/laya](https://github.com/receptron/laya) — MIT, 622 stars, weights by Convai Innovations under Apache 2.0.

It is an open-source, **Jev-compatible System-1 decision model** that runs on CPU
via ONNX Runtime. Its `systemOne` request/response shape is the same as Jev's, and
the project already speaks it: `jev.py:159-161` posts a `{"support": {"type":
"noul", …}}` question. Laya answers exactly that.

| Property | Value |
|---|---|
| Question types | `noul` (calibrated P(true)), `choice` (probability per option), `score` (expected level) |
| Latency | ~140 ms for a 3-question call on CPU, once warm |
| Weights | ~1.7 GB fp32, downloaded from Hugging Face on first use |
| RAM | ~2 GB loaded |
| Runtime | Node.js ≥ 20, `onnxruntime-node`; Python not required |
| Limits | ≤ ~20 options per `choice`; 192-token option budget; **512-token state** |
| Licence | MIT (code), Apache 2.0 (weights) |

### The honest finding: Jev is not broken

The user's premise was *"if jev isn't working, be honest"*. It is working. The
problems around it are different, and two of them are fixable without a new model:

1. **D5** — the budget is spent before a call that never happens. A code fix.
2. **D5's sibling** — the remaining real judgements go to Zen / OpenRouter
   *free tiers*, which rate-limit. A throttled judge is reported as
   `RATE_LIMITED` and a downstream exhaustion as `JUDGMENT_UNAVAILABLE`, which is
   why a real dataset shows 80 of 406 values with "No judgment".

**So the case for Laya is not repair, it is independence:** it would remove the
external rate limit entirely, at the cost of a 1.7 GB download and a Node
sidecar.

### What adopting it would actually require

- **State truncation is the binding constraint.** Our evidence text runs to
  12 000 characters; Laya's state is 512 tokens. For evidence-support this is
  acceptable — the *quote* is the evidence, and the current call already sends
  only `Claim` and `Evidence`. It would **not** be acceptable for Jev-C's
  free-form conflict adjudication, which Laya could only express as a `choice`
  over rival values — a real and arguably better encoding, but a different
  operation.
- **Integration shape**: Node 20 sidecar or subprocess, because the runtime is
  `onnxruntime-node` and the backend is Python. Startup cost is a model load, so it
  wants a long-lived process, not per-call.
- **Verification required before believing it**: run a labelled set of
  (claim, quote) pairs through Laya and through the current judge, and compare
  agreement — especially on the cases the deterministic short-circuit *does not*
  catch, which are exactly the hard ones.

I have **not** run Laya. Everything above is from its README and the shape
match. It is a credible option, not a verified one.

---

## 10. Corrections to the existing documentation

Every row was verified against the code.

| Claim | Where | Reality |
|---|---|---|
| `.env` is committed-readable; `.gitignore` is "proposed, not present" | `README.md:519`, `docs/37:126` | `.gitignore` exists, line 2 is `.env`, and `.env` is untracked. **The README's top-listed outstanding risk does not exist.** |
| `run_events` has no `type` column | `README.md:475`, `docs/14:45`, `docs/16:68`, `docs/37:95` | Migration 006 added it and the repo writes it. The compound claim leads with the false half; "no timestamp" is the part still true. |
| "every non-null field cites the exact stored text" | `README.md:4-5` | False on two paths (§5). |
| 389 / 361 / 143 backend tests | `README.md:458`, `docs/37:55`, `AUDIT.md` | **762.** |
| 16 / 13 / 14 / 11 endpoints | four files | **35.** |
| 13 SSE event types | `README.md:309`, `docs/16:14` | **14** in code; **13** wired to the browser (§D2). |
| "One known flaky test" | `README.md:485` | No known flaky test. |
| Graph nodes `search → collect → ask_jev → refine/finish` | `docs/30:5`, `docs/33`, `docs/34` | **Three nodes**: `search`, `screen`, `decide`. |
| "6 tool shims" | `docs/30`, `docs/34` | Four exist; `request_source_fetch`, `refine_search_query`, `request_research_retry` are nowhere in the repo, and `apply_decision` is not in the allowlist. |
| Human-in-the-loop `interrupt({...})` + `Command(resume=…)` | `docs/34:22` | **No `interrupt` in the codebase.** A REVIEW decision returns FETCH. |
| "Gemini primary, Groq fallback" | `docs/28`, `docs/33`, `ARCHITECTURE-DETAILED`, `docs/34` | Actual order is **OpenCode → Groq → Gemini** (`providers/llm/generate.py`). |
| `/api/history` returns runs joined with workflows + datasets | `docs/22:5`, `docs/04:11` | `SELECT * FROM runs`. No join. |
| Exact backend file tree (`routes_runs.py`, `schemas/dataset.py`, `providers/crawl/http.py`, …) | `docs/05:32-37` | None of those exist. All routes are in one 1321-line `main.py`. |
| Provider interfaces are the architecture seam | `docs/05:39` | `schemas/base.py` defines the Protocols and **nothing imports it**. |
| `extract_page(llm=None)` always returns `[]` | `docs/36:19` | The deterministic selector path runs *first* and returns records. The test passes because its fixture matches no selector. |
| Provenance source block is `{url, title, quote, retrieved_at}` | `docs/12:5-9` | Eight keys, including `page_id` and `start`/`end` — `page_id` is the point of the layer. |
| Verification statuses are 3 | `docs/12:9` | Five. `judgment_unavailable` and `rate_limited` are live. |
| "LLM calls sequential per page" | `docs/35:4` | Pages and chunks run concurrently. |
| `E_PROVIDER_FATAL` is 401/403 | `docs/35:3` | It is **424**. |
| `RUN_MAX_RUNTIME_S` = 1200 | `README.md:431` | **600.** |
| Latency table for a 21.5k-char prompt | `README.md:439`, `docs/23`, `docs/37` | No script or fixture in the repo produces it. |
| "on the last live run, 226/226 resolved" attached to a unit test | `README.md:148` | The test checks one quote on one synthetic page. |
| 4 e2e suites | `docs/37:59` | **9.** |

**Claims that are correct and should be kept:** no vector store; no
multi-tenancy; no CI; `REDUCING`/`FINALIZING` never emitted; the
`page_evidence_text` round-trip is real; the `{data, error, meta}` envelope is
real; the judge budget is genuinely per-run; Jina, docling and impersonation are
unreachable in this configuration.

---

## 11. How to verify any of this yourself

```powershell
# tests — note the interpreter; the venv has psycopg_pool, the system python may not
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m pytest backend/tests -q
#   → 762 passed

cd frontend
npm run build          # preview serves dist/, so rebuild after any src change
npm run test:charts
npm run e2e            # 9 suites, sequential, ~15 min

# the reuse defect, in one line
python -c "import ast;t=ast.parse(open('backend/app/main.py',encoding='utf-8').read());print([n.lineno for n in t.body if isinstance(n,(ast.Import,ast.ImportFrom)) and any(getattr(a,'asname','')=='politeness_svc' for a in n.names)])"
#   → []  (no module-level binding; main.py:1261 is function-local)
```

Working directory: `C:\Users\asus\Downloads\datagoblin`.
`.env` holds live credentials, is gitignored, and is not in history.
