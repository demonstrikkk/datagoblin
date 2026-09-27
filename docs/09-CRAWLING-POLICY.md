# 09 — CRAWLING POLICY (permitted sources only)

## Fetcher priority — what actually runs

1. **direct HTTP** (httpx + BeautifulSoup) — fast/cheap, Trafilatura markdown reduce
2. **impersonate** (curl_cffi TLS impersonation) — only if `IMPERSONO_ALLOWLIST`
   matches. Unset in this deployment, so this rung is inert.
3. **Crawl4AI** (+Playwright) — for JS-heavy pages, or an HTTP failure

Triage: `services/source_router.py::triage_source()` is pure string/extension
matching — no LLM, no probing. `web:http` is the default.

## Implemented but NOT reachable in a run

Stated plainly because the docs previously implied otherwise:

- **Jina Reader** — `fetcher.jina_fetch()` is complete and gated on
  `FEATURE_JINA`, but `"jina"` is absent from the crawl order table, so no
  dispatch path can ever select it. The `JINA_API_KEY` in `.env` is unused.
- **Docling** — `providers/crawl/docling.py` has zero callers. `FEATURE_DOCLING`
  is read by nothing. The `document:docling` and `structured:api` routes resolve
  to HTTP methods that hit the non-HTML guard, so **PDF and JSON sources are
  always skipped**.
- **Selenium** — not present in the repository at all. The browser path is
  Playwright via Crawl4AI, which covers JS rendering. Do not add Selenium
  without a reason Playwright does not serve.

## Robots gate at discovery

Candidates are checked for crawl permission **during discovery**, before the
Jev-A screen, and a disallowed URL is dropped from the candidate set and marked
screened so a later round never reconsiders it. The per-host robots cache (1h)
makes repeat checks effectively free, and placing the cheap check first means no
judge call is spent on a URL that was never going to be fetched.

One live run accepted two sources, both `robots-disallowed`, spent the entire
runtime budget, and returned 0 records with nothing to backfill from. The
crawler was right to refuse them; the waste was accepting them. Dropping them
at discovery frees the cap for the next candidate. An uncertain check (network
error) allows through — the fetcher re-checks anyway, and guessing "blocked"
would cost yield.

## Escalation to a renderer

Measured on the **clean text**, never on raw HTML (a JS shell is huge HTML and
tiny text):

| trigger | rule |
|---|---|
| `looks_js_shell` | clean text < `FETCH_THIN_CHARS` **and** `<script>` present |
| `has_hollow_code` | scripts present **and** an empty `<pre>`/`<code>` (runtime-injected) |
| `low_density_shell` | html > 20000 **and** clean/html < 5% **and** scripts present |

If every rung is thin, the longest page wins, best-effort, labelled by whichever
`method` produced it.

## Subpage traversal — two bugs that used to cripple it

1. **Rendered pages never expanded at all.** The gate read `page["html"]`, but
   Crawl4AI returns `markdown` and `rendered_html` and no `html` key — so a
   JS-heavy seed never enqueued a single child, exactly when following subpages
   matters most. Its parsed links were sitting unused in the dict. Both sources
   are honoured now.
2. **`RUN_MAX_DEPTH=2` meant one hop.** `depth + 1 < max_depth` evaluated
   `2 < 2` for a child and stopped. The limit now counts hops below the seed.

## Hard limits — corrected

`docs/09` previously advertised three limits **that no code enforced**:

| documented | reality |
|---|---|
| `max_requests_per_run=50` | **dead** — `RUN_MAX_REQUESTS` has zero readers |
| `max_pages_per_source=5` | **dead** — `RUN_MAX_PAGES_PER_SOURCE` has zero readers |
| `MAX_SEARCH_QUERIES=5` | **dead** — `RUN_MAX_SEARCH_QUERIES` has zero readers; the live budget is `SUPERVISOR_MAX_QUERIES` |
| `MAX_PAGES=15` | live: `RUN_MAX_PAGES` (12) |
| `MAX_DEPTH=2` | live: `RUN_MAX_DEPTH` — counts hops |
| `request_timeout=20s` | live: `FETCH_HTTP_TIMEOUT_S` (render: `FETCH_CRAWL_TIMEOUT_S`) |
| `MAX_RESULTS_PER_QUERY=5` | live: `RUN_MAX_RESULTS_PER_QUERY` |

The real per-host cap is `RUN_MAX_DOMAIN_PAGES` combined with the plan's
`traversal.max_pages_per_domain`.

Politeness is enforced inside the fetcher regardless: robots.txt (cached 1h,
fetched as our own UA), a 1.0s floor plus `Crawl-delay` and adaptive throttling,
and exponential backoff with jitter. No stealth/proxy/TLS-spoof/CAPTCHA-bypass
ever (see docs/03).

Never: bypass auth, bypass paywalls, solve CAPTCHAs, credential stuffing, follow
arbitrary external actions, execute page JS outside a browser sandbox.
