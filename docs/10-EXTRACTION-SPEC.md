# 10 — EXTRACTION SPEC

Input: the workflow schema (fields) + source URL + source title + the **stored**
page text (`reducer.page_evidence_text()` — the same definition the evidence
store uses, so quotes always resolve).

Output:

```json
{"records":[{"fields":{"company_name":"Acme AI","funding_stage":"Series A"},
  "evidence":[{"field":"company_name","quote":"Acme AI ...","source_url":"..."}]}],
 "coverage":"full"}
```

Rules: never return free-form prose; schema-typed values only; every non-null
field SHOULD carry a quote (missing → `unverified`, see docs/11).

## Concurrency — the reason a run produced nothing

Extraction used to be strictly sequential in **both** dimensions:

- chunks within a page, one after another, so a page cost the **sum** of its
  calls;
- pages within the run, one after another, with a spacing sleep between them.

Both are now concurrent (`asyncio.gather`, bounded by `EXTRACT_PAGE_CONCURRENCY`).
Measured on a live run: 8 pages were fetched and stored, then **every**
extraction returned `0 records (timeout)` because 4 sequential chunks could not
fit inside `EXTRACT_PAGE_TIMEOUT_S`. Concurrency turns the sum into the max.

Rate-limit pacing is **kept** — the free tier is per-minute — but applied in
waves (`idx % concurrency`), not as a flat `spacing * idx`, which made the last
page of a 12-page run wait 110s before it even started.

## Per-chunk failure

- one chunk fails → its result is dropped, the page keeps the others
- **every** chunk fails → provider `"error"`, not an empty page. Reporting "no
  records here" for a broken provider would claim a fetchable page was genuinely
  devoid of the entities.

## Regeneration

One bounded retry when the content demonstrably contains the field terms but
nothing was extracted — a miss, not an absence. Tripwire-cold pages are skipped
without a retry.

## Volume

A fast model on a rich page extracted **728 records** from 9 pages. That is the
point of the judge budget (`RUN_MAX_JUDGE_CALLS`, docs/11): the call count is a
function of how much was found, and must not be able to overrun the run.

Every record carries `source_url`, `source_title`, `page_id` and
`content_hash` forward into provenance.
