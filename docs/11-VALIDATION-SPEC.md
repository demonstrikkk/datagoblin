# 11 — VALIDATION SPEC (deterministic gates first, judge last)

Validation is a **gate**. A record that cannot satisfy its own required fields
is dropped, not stored as a row of nulls.

> This was not true until recently. `ItemPipeline` was fully built for dropping
> and the runner even looped over the `dropped` list, but no stage ever raised
> `DropItem`. Validation annotated failures; it did not enforce them, and
> `record.rejected` fired 129 times against 0 dropped rows.

## Order of checks

Cheapest first, because the judge is the only step that costs a round trip and
the only one that can be unavailable.

1. **Value present** → else `unverified`, value `null`
2. **Type matches the declared type** → else `unverified`
3. **Quote is a substring of the stored page text** (whitespace/case-normalised)
   → else `unverified`. This is the hallucination guard.
4. **Citation reference exists** in the page's References map → else `unverified`
5. **Required fields present** → else the whole **record** is dropped
6. **Judge** (or a deduction) → the final word on the claim

## Types

`string`, `number`, `boolean`, `date`, `url`, `array`. Normalization is code,
not LLM.

`required` and `type` used to be discarded outright (`_ = required, ftype`) on
the grounds that "coercion lives in normalize" — but normalize only *transforms*
a value and never reports failure, so a string in a number field passed as
verified. Both are now enforced. The number gate accepts scraped shapes
(`$2.4B`, `1,200`, `45%`, `~30`, `1.2 million`) and rejects prose
(`about 240`, `240 employees`); a too-strict gate would drop real records at the
door rather than catch anything.

## Verdict statuses

| status | meaning |
|---|---|
| `verified` | judged, or deduced (see below) |
| `unverified` | not supported by a quotable page; value is `null` |
| `conflicting` | two sources disagree; both provenances kept, never averaged |
| `judgment_unavailable` | the quote **is** on the page, but no judge was reachable |
| `rate_limited` | the judge was there and said "wait" (HTTP 429) |

`judgment_unavailable` is deliberately not `verified`. The judge used to return
`SUPPORTED` whenever it was unreachable, so a dead or unconfigured judge
silently promoted every substring match to "verified" — and because no test
stubbed the judge, **the entire suite was passing on that bug**. A judgement
that never happened is now reported as one that did not happen.

## Two ways a field becomes `verified` without a judge call

1. **Verbatim containment.** If the value appears inside its own quote, "does
   this evidence support the claim" has no answer but yes. Spending a model
   round trip on it is pure latency: a live run reached 728 extracted records,
   which is thousands of calls and a blown budget. This is a deduction from the
   substring, not a guess.
2. **The judge says so** (`score >= 0.6`).

The 0.4–0.6 band is a real "cannot tell" and maps to `judgment_unavailable`. It
used to be treated as support.

## The judge budget

`RUN_MAX_JUDGE_CALLS` (default 400) caps judge calls per run. Past the cap,
fields are kept but marked `judgment_unavailable` — never counted as verified.

A throttled judge is reported as `rate_limited` rather than `judgment_unavailable`:
"come back later" and "we had no judge to ask" call for different responses, and
collapsing them made a busy run look like one with poor evidence. The transport
retries 429s with exponential backoff before giving up (docs/32).
Uncapped, the call count is a function of how much the extractor found, which
is not something a run should be able to overrun on.

## Reporting

Counts are named for what they measure, because the old summary was
`{"records":1,"verified":0,"needs_review":4}` — which reads as four bad records
when it is one record with four unproven fields.

```
records, records_fully_verified, records_needing_review,
fields_verified, fields_unverified, fields_judgment_unavailable,
fields_conflicting, fields_rate_limited
```

## Events

- `record.rejected` — the record was **dropped**. Means it.
- `record.needs_review` — the record was **kept** with unproven fields.
- `record.verified` — every field verified.
