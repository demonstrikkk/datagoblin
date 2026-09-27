# 19 — ERROR HANDLING

| Failure | Behaviour |
|---|---|
| Search API fails | auth/config failure → zero results, run continues on seed URLs; transient propagates and the bounded loop retries next round |
| URL timeout | retry per `RUN_RETRY_COUNT` → alternate fetch method → skip source, mark failed |
| HTTP 403 | try the permitted browser fetch (Crawl4AI) → skip if still blocked, never circumvent |
| Malformed page | skip source, record error |
| **Evidence cannot be stored** | the page is counted as **failed**, not kept as an unverifiable success |
| Extraction: one chunk fails | drop that chunk, keep the page's other records |
| Extraction: all chunks fail | provider `"error"` — not an empty page |
| Extraction stalls | cut off at `EXTRACT_PAGE_TIMEOUT_S`, page skipped, run continues |
| Invalid schema output | reject the extraction, do not store |
| Record missing a required field | **record dropped** (`record.rejected`) |
| Record kept, fields unproven | kept, marked `record.needs_review` |
| Judge unreachable | `judgment_unavailable` — **never** silently "verified" |
| Judge budget spent | `judgment_unavailable` for the remainder |
| Conflict judge fails | cell stays `conflicting`; a conflict is never resolved one way |
| Duplicate | merge, union provenance, emit `duplicate.merged` |
| All sources fail | `FAILED` run |
| **Some work done, budget exhausted** | `PARTIAL`: validated records **stored**, run marked `FAILED + partial=true` |
| OpenCode server not running | fail fast, no retry — a refused connection cannot succeed on retry |
| Upstream 5xx / 429 | bounded retry with backoff (transient only) |

## Honesty rules

These exist because the code previously did the opposite:

- The old timeout path emitted `Runtime budget exceeded (600s); partial kept`
  while `store()` was never reached. **Every extracted record was discarded**
  and the message described the opposite of what happened. Now records are
  published per page and stored on timeout, and the count reported is the count
  actually kept — including **zero**, with a message that says so.
- A partial run is `FAILED` with `partial = true`. It is never `COMPLETED`,
  even though a dataset was written.
- A run that finds nothing says nothing was found. It does not report a
  confident negative derived from a page it could not read.

## Error shape

All errors use `{data, error:{code,message,details}, meta}`.

FastAPI's own validation errors used to leak `{"detail":[...]}` — a different
shape from every other response — and the frontend read only `error.code`, so a
422 surfaced as the bare text `E_VALIDATION` with the reason discarded, which
looked like a silent failure. A `RequestValidationError` handler now returns
the standard envelope, and the client surfaces the message from either shape.

Every failure writes `run_events` + `sources.status`/`error`. No silent drops.
