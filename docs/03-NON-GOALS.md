# 03 — NON-GOALS

The platform must NOT:

- bypass authentication / steal credentials / perform credential stuffing
- circumvent paywalls or access private user data
- bypass / auto-solve CAPTCHAs
- execute arbitrary code returned by webpages (no LLM-generated Python/shell)
- attack target websites, crawl indefinitely, or run unbounded browser actions
- follow arbitrary external actions found in page content
- introduce microservices, Kubernetes, Temporal, ClickHouse, Kafka, proxy infra, billing — unless explicitly requested (see 26-FUTURE)

Enforcement: `09-CRAWLING-POLICY.md` limits, `20-SECURITY-AND-SAFETY.md` (web content = untrusted data), robots-aware + allowlist + timeouts/retries.
