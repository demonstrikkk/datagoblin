# 02 — MVP SCOPE

## MUST HAVE

- [x] Prompt input
- [x] AI plan generation + plan review (compile → preview → run)
- [x] Search discovery (bounded queries, Tavily)
- [x] Public web fetching (HTTP waterfall → Crawl4AI)
- [x] Dynamic-page fallback (Crawl4AI/Playwright)
- [x] Content reduction (HTML → clean Markdown)
- [x] Structured extraction (schema + clean content → records + evidence)
- [x] Validation (Pydantic + evidence check)
- [x] Evidence / provenance (cell-level)
- [x] Deduplication (exact → fuzzy; semantic optional)
- [x] Dataset table + search/filter
- [x] Proof drawer (click cell → source/quote/timestamp)
- [x] Run progress (SSE timeline + activity feed + counters)
- [x] Run history (prompt + plan + trace + dataset)
- [x] CSV + JSON export (Parquet optional)
- [x] Cancel run, retry failed source, source health counts
- [x] Demo replay fallback mode

Full checklist in spec §42 (22 items). Anything not listed here needs explicit approval.

## SHOULD HAVE (still MVP if cheap)

- Cancel run, retry failed source, source health (attempted/successful/failed)
- Dataset statistics (records, fields extracted, verified/needs-review)
- Replay demo, Qdrant semantic dedupe (optional, app works disabled)

## NOT NOW (see 03-NON-GOALS, 26-FUTURE)

Billing, Teams, RBAC, SSO, Kubernetes, Temporal, ClickHouse, proxy marketplace /
residential proxies, scheduling, enterprise audit, MinHash-at-scale, Stripe.
