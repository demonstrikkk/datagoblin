# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a **data engineer who already scrapes** and has been unable to
defend an extracted field when it was questioned. They can already write a spider or
call a search API; getting HTML was never their problem. Their problem is a review,
a stakeholder, or an audit where they were asked *"where did this number come from?"*
and the honest answer was "the model said so."

Secondary audience: engineers evaluating the project on its engineering merit, who
read the architecture and the honest-limitations list before deciding.

Explicitly **not** the target: people who want the largest volume of cheap, fast,
unverified JSON. That visitor experiences the product's central rigor as friction and
will bounce.

## Product Purpose

DataGoblin turns a natural-language question into a dataset in which every non-null
field carries the exact stored text it came from — a quote, character offsets, a
`page_id`, a content hash, and a retrieval timestamp — or the field stays null.

It exists because an LLM asked to produce structured data will confidently produce
values it cannot support, and the usual pipeline has no way to notice: the page was
fetched, reduced in memory, extracted from, and discarded, so nothing survived to
check the claim against.

Success means a reader can pick any cell and land on the sentence that justifies it,
and that cells with no such sentence are visibly empty rather than plausibly filled.

## Positioning

Not a scraper and not an agent framework — an **evidence and provenance pipeline**.

The differentiator is a stored substrate: every crawled page is persisted as
`markdown` + `raw_html` + `content_hash` + `parent_url` + `depth` + `retrieved_at`
*before* anything is extracted from it, and `reducer.page_evidence_text()` is the
single definition of quotable text shared by the store and the extractor. That is what
makes a quote re-checkable rather than merely present. A test slices the stored
markdown at the cited offsets and requires the result to equal the quote.

A neighboring product could copy "cites its sources" as a claim. It cannot cheaply
copy a substrate whose cited offsets provably still resolve.

## Operating Context

A local FastAPI backend plus a React operator console. The console is where the work
happens: ask → compile a plan → run → watch a live SSE timeline → inspect a finished
dataset → open a proof drawer on any cell → export CSV/JSON/JSONL/Markdown.

Runs take 15–45 seconds. Latency is dominated by the extraction model
(`OPENCODE_MODEL`; `big-pickle` measured at 4.9s versus 29.2s–170.2s for slower free
tiers). Judges run through a local `opencode serve`; where no judge is reachable every
family degrades to a stated deterministic outcome rather than a fabricated judgment.

This is an operator tool used at a desk by someone reading a terminal, in daylight —
which is why the incumbent world is warm paper and ink rather than dark chrome.

## Capabilities and Constraints

- Pipeline: planner → search (Tavily) → robots gate → Jev-A source screening →
  crawler (4 workers, depth-limited BFS) → **store** → reducer → extractor (LLM,
  concurrent) → validator → deduper (L1 exact + L2 RapidFuzz token_set) → atomic finalize.
- Verification statuses are a real vocabulary, not a boolean: `verified`,
  `unverified`, `conflicting`, `judgment_unavailable`, `rate_limited`.
- The judge returns a judgment with probabilities and **Python applies the threshold**.
  `0.4–0.6` is a genuine "cannot tell" → `UNCERTAIN`, never `verified`.
- `judgment_unavailable` is deliberately not `verified`: the quote is on the page but
  nothing ruled on it.
- Dedup merges **union provenance**; rivals are preserved rather than overwritten.
- Persistence is an explicit `PERSISTENCE` switch (`postgres` default via psycopg,
  `local` JSONL opt-in) and the active adapter is reported by `GET /api/health`, so a
  silent downgrade cannot go unnoticed.
- Permitted sources only: robots.txt honored, per-host throttle honoring
  `Crawl-delay`, exponential backoff with jitter, domain allowlist, full SSRF check.
- Never: bypass auth or paywalls, solve CAPTCHAs, credential stuffing, TLS spoofing,
  proxies, or executing page JS outside a browser sandbox.
- Known-bad behavior is documented rather than hidden; the project maintains a
  current-state doc and an audit with an addendum of corrections.

## Brand Commitments

- Name: **DataGoblin**.
- Voice: an engineer who has been burned and refuses to overstate. Short declarative
  sentences. "No `TODO`s ship as done work." Limitations are a feature of the copy,
  not a disclaimer section.
- Established visual world (inherited, not to be reinvented): warm paper + ink, a
  single bronze accent, a serif display face against a monospace for anything
  machine-derived, near-opaque data surfaces over an ambient field, and the
  signature **proof highlight** — a value shown sitting inside the sentence that
  supports it.
- The brand's characteristic move is the cited artifact: the quote, the offsets, the
  honest null.

## Evidence on Hand

Real, on disk, and usable as demonstration material:

- `outputs/run_0ad4935d-…json` — a genuine completed run (prompt: top 8 Indian
  companies ≥ ₹100 crore, FY 2024-25). 123 records, 369 fields, **196 `verified` /
  173 `unverified`**, real quotes and offsets against a real Wikipedia page. Contains
  both verified values (`Reliance Industries Limited` → `997,795`, quote `997,795`) and
  honest nulls (`valuation: null`, `unverified`, empty quote) — the exact contrast the
  product thesis is about.
- `outputs/export.csv`, `outputs/summary.md`, `outputs/events.jsonl`, `outputs/report_*.md`.
- `.datagoblin-local/*.jsonl` — full local run history including events and sources.
- `docs/37-CURRENT-STATE.md`, `AUDIT.md`, `README.md` — measured facts, latency tables,
  and an honest-limitations list.
- `frontend/datagoblinlogo.png` and public brand assets.

**Do not fabricate:** customers, testimonials, logos, pricing, uptime or throughput
benchmarks, coverage percentages, or a "10,000 records scraped" claim. There are none.

`outputs/dataset.json` is a **synthetic fixture** (example.com URLs, invented
companies). Real material must come from the Wikipedia run, not this file.

## Product Principles

1. **No claim without a stored citation.** If the sentence is not stored, the value is
   not defensible, so it does not ship.
2. **Absence is an honest outcome.** A null, a `0`, or a `partial` flag is a real,
   queryable result — never an error to be papered over.
3. **A check that cannot fail is not a check.** The project's worst historical bugs were
   all verifications that silently passed; tests now assert the *absence* of false
   claims.
4. **Deterministic in the spine, bounded agentic at the edges.** LLMs understand and
   generate; Python executes policy and mutates state.
5. **Document the damage.** Known limitations are published with evidence and ranked
   causes, because "it works" is the claim that hid the most.

## Accessibility & Inclusion

Keyboard-reachable throughout with visible focus rings; `prefers-reduced-motion`
disables decorative movement while keeping state legible; `forced-colors` mode carries
evidence by underline rather than by a color nobody can see; contrast is guaranteed by
near-opaque data surfaces rather than by whatever the ambient background is doing.