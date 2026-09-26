# 09 — CRAWLING POLICY (permitted sources only)

## Fetcher priority — MVP (lean): HTTPX → Crawl4AI. Later adapters (not implemented): Docling, Jina, BrowserFetcher.

MVP:
1. direct HTTP (httpx + BeautifulSoup) — fast/cheap, with Trafilatura markdown reduce
2. Crawl4AI (+Playwright) — JS-heavy or HTTP 403/timeout fallback

Later (interface-ready, Phase 1.5/3 — do not build now unless demo needs PDFs):
3. document branch (Docling — PDF/DOCX/XLSX)
4. Jina Reader — difficult-page fallback
5. BrowserFetcher (Stagehand / Browser Use — last resort)

Triage: `backend/app/services/source_router.py::triage_source()` (MVP: http default, crawl4ai on js-heavy hint). Runner tries http→crawl4ai, then skip. No stealth/proxy/TLS-spoof/CAPTCHA-bypass ever (see 03).

Never: bypass auth, bypass paywalls, solve CAPTCHAs, credential stuffing, follow arbitrary external actions, execute page JS outside browser sandbox.

## Scope

In scope: public pages/APIs/search results, public job/company/news/docs pages, RSS/feeds. Out: authenticated/private, paywalled, credential collection, private user data. Robots-aware, domain allowlist (`allowed_sources`, `seed_domains`), rate limits.

## Hard limits

`max_requests_per_run=50`, `max_pages_per_source=5`, `request_timeout=20s`, `retry_count=2`, plus flow caps: `MAX_SEARCH_QUERIES=5`, `MAX_RESULTS_PER_QUERY=5`, `MAX_PAGES=15`, `MAX_DEPTH=2`.

## Failure handling (see 19)

URL fail → retry once → alternate fetch method → skip source → continue run. Report `sources_attempted/successful/failed` on dataset.
