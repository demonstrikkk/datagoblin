# 09 — CRAWLING POLICY (permitted sources only)

## Fetcher priority — what actually runs

1. **direct HTTP** (httpx + BeautifulSoup) — fast/cheap, Trafilatura markdown reduce
2. **impersonate** (curl_cffi TLS impersonation) — only if `IMPERSONO_ALLOWLIST`
   matches. Unset in this deployment, so this rung is inert.
3. **Crawl4AI** (+Playwright) — for JS-heavy pages, or an HTTP failure

Triage: `services/source_router.py::triage_source()` is pure string/extension
matching — no LLM, no probing. `web:http` is the default.

## Flag-gated rungs (default off, reachable when enabled)

- **Jina Reader** — `fetcher.jina_fetch()` trails web routes when `FEATURE_JINA`
  is on (`crawler._methods_for`). Off by default; the rung refuses by name when
  disabled.
- **Docling** — `fetcher.docling_fetch()` leads `document:docling` routes when
  `FEATURE_DOCLING` is on and the optional package is installed. PDFs routed to
  it with the flag off keep the honest `non-html` skip. `structured:api`
  (JSON endpoints) is served by the HTTP rung's `json_text` path plus the
  deterministic `jsonrung` — no model call for complete payloads.
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
`method` produced it — **unless it is still unreadable**, in which case the page
raises `E_RENDER_FAILED` and is counted as failed.

That last clause matters. Returning a thin shell as `best` let the extractor mine
`NA` out of an empty `<div id="root">` and store those as real records:
confident, wrong, and indistinguishable from good data. `/api/intel/ask` already
refused this case; the run pipeline did not, and had no equivalent.

## Content behind a click

The renderer runs `_EXPAND_JS` before markdown is generated. It clicks **reveal**
controls — "load more", "show more", "read more", `aria-expanded="false"`
accordions — up to `_MAX_EXPAND_CLICKS` (4) times, then scrolls to the bottom
after each. Three deliberate limits:

* **Reveal only.** A click on a real `href` navigates and throws away the page
  being read, so navigable links are skipped.
* **Never consent controls.** Cookie banners and sign-up prompts sit in the same
  visual layer as reveal buttons; `cookie|consent|gdpr|subscribe|sign up|log in`
  are excluded by label.
* **Bounded.** Each click is a round trip against a third party. Four is a
  safety rail against a "load more" that never disables itself, not a tuning knob.

`scan_full_page=True` remains the lazy-load trigger for images and
`IntersectionObserver` reveals. Neither it nor `_EXPAND_JS` can reach content
behind an XHR call or a custom-routed SPA path.

## JS-only navigation

`extract_links_with_text` harvests destinations that have no `href`, because on a
rendered card grid they are most of the links on the page: `data-href`,
`data-url`, `data-link`, `data-next`, and a path extracted from an `onclick`
handler. Same-host and `http(s)`-only, as before.

## Subpage traversal — two bugs that used to cripple it

1. **Rendered pages never expanded at all.** The gate read `page["html"]`, but
   Crawl4AI returns `markdown` and `rendered_html` and no `html` key — so a
   JS-heavy seed never enqueued a single child, exactly when following subpages
   matters most. Its parsed links were sitting unused in the dict. Both sources
   are honoured now.
2. **`RUN_MAX_DEPTH=2` meant one hop.** `depth + 1 < max_depth` evaluated
   `2 < 2` for a child and stopped. The limit now counts hops below the seed.
3. **The budget was spent on seeds before traversal began.** Seeds and children
   drew on one pool, and seeds won: discovery offers up to `max_pages` seeds,
   `_worker` counts every claim against that same number, and seeds are exempt
   from the per-domain cap. A normal run therefore entered the queue with
   `seeds == budget` and drained without a single child ever being popped —
   `RUN_MAX_DEPTH` was reachable, honoured, and completely unused.
   `SEED_BUDGET_SHARE` (40%) of the budget is now reserved for seeds, so
   traversal always has room.
4. **Pagination was unreachable.** `page`/`offset`/`start` sat in
   `_NAV_QUERY_KEY_KEYS` for `-4.0`, which put `?page=2` below the `score >= 0`
   cut in `_rank_children`. A listing was fetched once and the rest of its
   records discarded, while every page fetched was reported as a success. They
   now score as continuations, path-style `/page/2` is recognised (it carries no
   query string at all), and `_rank_children` reserves a per-parent slot for the
   best continuation — on a page of author links `/page/2/` tied with all of them
   and lost every slot to the alphabetical tiebreak.
5. **Rendered links were thrown away.** `_crawl_result_links` emits
   `{"url","text","internal"}` and `_child_links_with_text` had no branch for
   that shape, so every link the renderer had parsed — including post-JS links no
   static parse can see — was discarded. Children survived only via a second
   parse of `rendered_html`, capped at 50 same-host links, so the renderer's 200
   collected links bought nothing.

## Truncation is reported, never silent

Every cap that can end a crawl early is counted and returned in
`counts["traversal"]`:

| key | meaning |
|---|---|
| `seeds_queued` / `seeds_dropped` | how many seeds the reserve trimmed |
| `traversed_pages` / `depth_reached` | whether the crawl actually left its seeds |
| `blocked_by_domain_cap` | children refused by the per-domain cap |
| `blocked_by_budget` | children never reached because the budget was spent |
| `per_domain_cap`, `budget`, `max_depth` | the caps in force |

`coverage` attributes missing data to "crawl-depth". That was unfounded while a
flat crawl reported the same numbers as a deep one; a run can now show which cap
actually stopped it.

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
`traversal.max_pages_per_domain` (default **10**, raised from 3 — 3 pages of one
host was low enough to block recursion into the single most relevant site).

Politeness is enforced inside the fetcher regardless: robots.txt (cached 1h,
fetched as our own UA), a 1.0s floor plus `Crawl-delay` and adaptive throttling,
and exponential backoff with jitter. No stealth/proxy/TLS-spoof/CAPTCHA-bypass
ever (see docs/03).

Never: bypass auth, bypass paywalls, solve CAPTCHAs, credential stuffing, follow
arbitrary external actions, execute page JS outside a browser sandbox.
