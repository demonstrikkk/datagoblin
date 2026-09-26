# 19 — ERROR HANDLING

| Failure | Behaviour |
|---|---|
| Search API fails | retry once → fallback provider → if still failing, FAIL run with error |
| URL timeout | retry once → alternate fetch (HTTP↔Crawl4AI) → skip source, mark failed |
| HTTP 403 | try permitted browser fetch (Crawl4AI) → skip if still blocked, never circumvent |
| Malformed page | skip source, record error |
| Extraction fails | retry once → skip page |
| Invalid schema output | reject extraction (do not store) |
| No evidence | value=null, unverified (never store guess) |
| Duplicate | merge, union provenance, emit events |
| All sources fail | FAILED run |
| Some sources fail | partial completion + sources_attempted/successful/failed |

Every failure writes run_events + sources.status/error. No silent drops.
