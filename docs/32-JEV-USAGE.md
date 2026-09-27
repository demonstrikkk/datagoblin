# 32 — JEV USAGE MAP (CORE decision layer: 4 families)

Jev = typed judgments (Choice/Score/Noul + probabilities), **not** prose
generation and **not** a pipeline stage.

```
LLM    = understand / generate
Jev    = judge / decide
Python = execute
DB     = remember
```

Jev returns a judgement. **Python applies the policy.** Jev never mutates state
and never decides what gets stored. Outputs are probabilities; the deterministic
policy maps them onto `verified` / `unverified` / `conflicting` /
`judgment_unavailable` — never "Jev says TRUE".

## A. Source screening — `jev.source_screening()`

Q: is this source relevant? → `YES` / `NO` / `UNCERTAIN`. `NO` skips
fetch+extract entirely. Runs in concurrent batches of 5; the sequential version
stalled whole runs at ~10s a call.

## B. Evidence verification — `jev.evidence_verification()`

Q: does this quote support this claim? → `SUPPORTED` / `NOT_SUPPORTED` /
`UNCERTAIN` / **`JUDGMENT_UNAVAILABLE`**.

Order: deterministic substring pre-check, then a verbatim-containment
shortcut, then the judge. See docs/11.

> **The bug this family hid:** when no judge was reachable, this returned
> `SUPPORTED, 0.9, deterministic`. A dead or unconfigured judge therefore
> promoted every substring-matching claim to "verified", and the verification
> summary counted them as checked. Because no test stubbed the judge, the whole
> suite was passing on that behaviour. It now returns
> `JUDGMENT_UNAVAILABLE`, which the validator records as its own status and
> never counts as verified.

> **Throttled is not absent.** A 429 raises `JevRateLimited` after bounded
> exponential backoff (honouring `Retry-After`, capped at 30s) and is recorded as
> `RATE_LIMITED` → stored status `rate_limited`. It deliberately does **not**
> fall through to the paid OpenRouter rung: throttling is a "wait", and
> billing a paid call because the free tier is busy is a surprise charge.
> Counters are live at `GET /api/health` under `jev`.

## C. Conflict resolution — `jev.conflict_triage()`

Q: which evidence state? → `A` / `B` / `CONFLICT` / `INSUFFICIENT`. `CONFLICT`
marks the cell `conflicting` and never merges. Judged **concurrently** — a live
run merged 35 duplicates and then spent the rest of its 600s budget
adjudicating them one at a time.

A judge that *fails* leaves the cell `conflicting`. A conflict is never
silently resolved one way.

## D. Research continuation — `jev.research_continuation()`

Q: is the evidence enough, and if not, what now? → `sufficient` / `insufficient`
/ `uncertain`, with `FETCH` / `REFINE` / `REVIEW`.

> This was a pure function of `valid >= requested` with **no model call at
> all**, while the graph documented it as the coverage judge. Counting is
> necessary but not sufficient: 3-of-5 with no leads left should stop, 4-of-5
> missing a required field should continue, and neither is visible to a count.

It is now a real judgement, with the count comparison kept as the fallback so a
missing judge never changes behaviour — only its speed. The judge is told what
discovery actually knows (`_coverage_summary`: rounds used, queries run,
candidates seen, sources accepted, their titles), because it used to be invoked
with `valid_count` and `missing_fields`, **neither of which any node ever
wrote** — so it only ever saw "0 valid out of 20" and had nothing to reason
from.

The judge advises; `supervisor_step` still decides, and the loop is bounded by
`SUPERVISOR_MAX_ITERATIONS`.

## Not called anywhere

`jev.coverage_gate()` (legacy alias), `jev.source_relevance()`,
`jev.decide_record()` have **zero callers** in `app/`.

## Forbidden

Planning, extraction prose, validation math, dedupe, normalization, export,
transitions, budgets. Key absent → deterministic policy. Where a judge is
unavailable, every family degrades to a stated deterministic outcome rather
than a fabricated judgment.
