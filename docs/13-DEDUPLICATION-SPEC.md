# 13 — DEDUPLICATION SPEC

Levels, in order. The method is **never** "ask an AI if these are duplicates".

- **L1 Exact** — normalize (lowercase, trim, strip `Ltd/Inc/LLC/pvt/corp/co`,
  strip non-alphanumerics) then exact match on `dedupe_keys`. Match → merge,
  union provenance.
- **L2 Fuzzy** — RapidFuzz `token_set_ratio` >= 88 on the same keys
  ("Acme AI" vs "Acme Artificial Intelligence"). Candidate merge, both quotes
  kept.
- **L3 Semantic** — **not implemented.** `FEATURE_SEMantic_DEDUP=true` raises
  *by design* before any merge work, because there is no embedding provider
  wired. `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION` exist in config
  and are read by nothing. Leave the flag off; L1/L2 are the product.

## The dedupe-key fallback is weak

If `plan["dedupe_keys"]` is empty, the key silently becomes **every field name,
sorted** — a materially different and much weaker identity. The LLM planner
often emits no `dedupe_keys` for a generic plan, so this fallback is the common
path rather than an edge case. Worth fixing at the planner, not here.

An empty signature never merges: rows with no key values are all kept.

## Merge and adjudication

Fills empty incumbent cells. On a genuine conflict the incumbent value is kept,
`verification_status` is set to `conflicting`, and the rival is stashed under
`rivals`. Those go to **Jev-C** (`conflict_triage`), concurrently: `B` adopts
the rival value + source, `INSUFFICIENT` nulls and marks unverified, `A`/`CONFLICT`
leave the incumbent standing as `conflicting`.

A second, earlier dedup runs across chunk batches inside the extractor
(`merge_records`, keyed on sorted normalized field/value pairs plus quotes,
first occurrence wins).

## Scope limit — per-run only

Deduplication applies **only within one run's in-memory list**.

- No cross-run or cross-dataset dedup.
- `dataset_records` has **no unique index**, and the insert has no `ON CONFLICT`,
  so identical rows from two runs are stored twice.
- `seen_fingerprints` dedups **URLs**, not records.

Emits `duplicate.merged` (`duplicate.detected` was declared and never emitted).
