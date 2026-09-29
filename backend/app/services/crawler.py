"""Fetch orchestration: triage route -> MVP order -> backoff retry -> alternate -> skip.

Adds bounded in-domain traversal (depth < RUN_MAX_DEPTH, per-domain cap from
plan.traversal.max_pages_per_domain, global RUN_MAX_PAGES): same-host links found
in fetched pages are fetched as depth+1. Skipped pages (robots/non-HTML) are
persisted as skipped — normal, never failures. Semaphore(4) + per-host throttle
inside the fetcher bound concurrency and rate.

Phase-3: error STRATEGY (politeness.strategy) decides retry-vs-escalate for
non-fatal errors — escalate skips the backoff and tries the alternate method
immediately. The pick order is host-round-robin (least-settled host first)
so one slow domain cannot stall the crawl. Traversal links are filtered by
extension denylist + plan domain allowlist (seed_domains/allowed_sources).

Concurrency (Crawlee AutoscaledPool spirit, fixed size): 4 workers pull from
the shared priority queue. Per-host politeness lives in the fetcher (locks +
throttle), so it holds under concurrency. The budget can never overshoot:
a worker only picks while settled + in-flight < budget, and the claim
(seen.add + in_flight += 1) happens in a sync section — race-free on one
event loop. Page ORDER is nondeterministic; counts and per-URL semantics
are exact (tests assert sorted/counts, never order).
"""

_WORKERS = 4
import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from app.core.config import settings
from app.core.constants import RunStage
from app.core.errors import AppError
from app.providers.crawl import fetcher
from app.services import impersonation as impersonation_svc
from app.services import politeness as politeness_svc
from app.services import reducer as reducer_svc
from app.services.source_router import triage_source

_ORDER = {"web:http": ("http", "crawl4ai"),
          "web:crawl4ai": ("crawl4ai", "http"),
          "document:docling": ("http", "crawl4ai"),  # Later: docling first (FEATURE_DOCLING)
          "structured:api": ("http",)}

# Traversal never follows data/binary assets (Scrapy IGNORED_EXTENSIONS spirit).
# Seeds still route normally (e.g. an explicit .pdf seed reaches docling).
_IGNORED_TRAVERSAL_EXTS = frozenset(
    "zip tar gz rar 7z exe dmg pkg msi mp3 mp4 avi mov wmv "
    "jpg jpeg png gif svg webp ico css js woff woff2 ttf "
    "pdf doc docx ppt pptx xls xlsx".split())


def _methods_for(route: str, url: str) -> tuple[str, ...]:
    """Route order with the Phase-2 impersonation rung spliced in.

    Impersonation sits after plain httpx and before Crawl4AI (cheaper than a
    browser) — but ONLY for allowlisted hosts. Off-list URLs keep the exact
    MVP order; the rung can never fire without an explicit allowlist entry.
    """
    methods = list(_ORDER.get(route, ("http", "crawl4ai")))
    if ("http" in methods and "impersonate" not in methods
            and impersonation_svc.is_allowlisted(url)):
        methods.insert(methods.index("http") + 1, "impersonate")
    return tuple(methods)


def _domain(url: str) -> str:
    """Registrable-ish host for per-domain caps and round-robin.

    A leading `www.` is stripped because it is a DNS convention, not a
    different site: one run spent fifteen candidate slots on `csr.gov.in` as
    `http://`, `https://`, `www.`, trailing-slash and query-string variants,
    each counting as its own domain against the per-domain cap.
    """
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def canonical_url(url: str) -> str:
    """The URL as the *server* sees it: no fragment, no entities, no campaigns.

    A fragment is resolved by the browser, never sent to the server, so
    `page`, `page#/about-us`, `page#/login` and `page#/` are one resource.
    Treating them as distinct wasted a whole page budget on a single document:
    one live run stored five "pages" that were byte-identical copies of the
    same 7,915-character response, 80% of its budget, and extracted nothing
    because four of the five were the same nav-only page.

    `&amp;` is also decoded: it arrives as a literal entity from scraped HTML,
    and `?a=1&amp;b=2` is a different key to the server than `?a=1&b=2`.

    Campaign and affiliate parameters are dropped, and the `?` with them when
    nothing is left. The document served is identical either way, so
    `…/companies?utm_source=x`, `…/companies?gclid=y` and `…/companies` are one
    resource. Replay of this instance's stored pages found the same listing
    under `partner_category`/`partner_medium` labels, each of which would have
    taken a page budget of its own — and, because the reuse fingerprint is taken
    from the canonical form, stripping them also lets a re-run recognise the
    page it already has.

    Other query strings are preserved: they genuinely select content.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    raw = raw.replace("&amp;", "&").replace("&#38;", "&")
    try:
        p = urlparse(raw)
    except ValueError:
        return raw.split("#", 1)[0]
    if not p.netloc:
        return raw.split("#", 1)[0]
    if p.query:
        try:
            kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                    if k.lower() not in _TRACKING_QUERY_KEYS]
        except ValueError:
            kept = []
        p = p._replace(query=urlencode(kept))
    return p._replace(fragment="").geturl()


def _plan_domains(plan: dict) -> list[str]:
    """Domain allowlist for traversal from seed_domains + allowed_sources.

    Entries may be bare domains or full URLs; matching is exact-or-suffix.
    Empty (usual case) means no allowlist filtering.
    """
    out: list[str] = []
    for entry in (plan.get("seed_domains", []) or []) + (plan.get("allowed_sources", []) or []):
        entry = str(entry or "").strip().lower()
        if not entry:
            continue
        host = urlparse(entry).hostname if "://" in entry else entry
        host = (host or entry).lower().rstrip(".")
        if host and host not in out:
            out.append(host)
    return out


def _domain_allowed(host: str, allowed: list[str]) -> bool:
    return (not allowed or any(host == a or host.endswith("." + a) for a in allowed))


#: Path segments that almost never carry the records a plan is looking for.
#: Measured on this instance: 19% of every page ever fetched (28 of 151) was
#: one of these or a bare root, and none of them contributed a record. The
#: budget is finite, so a login page is a slot a real listing page did not get.
_NAV_SEGMENTS = frozenset("""
about about-us contact contact-us login signin sign-in signup sign-up register
privacy privacy-policy terms terms-of-service legal cookies cookie-policy
careers jobs employment blog news tag tags category categories search
cart checkout account my-account help faq support subscribe newsletter press
media events sitemap feed rss share print wishlist returns shipping
""".split())

#: Query keys that are navigation state rather than a distinct resource. Two
#: pages that differ only by `?page=2` are one resource twice, and
#: `canonical_url` strips fragments but not these.
_NAV_QUERY_KEYS = frozenset({"page", "offset", "start", "sort", "orderby", "order",
                             "filter", "q", "search", "ref", "from", "view"})

#: Campaign and affiliate parameters. The destination is unchanged by them, so a
#: link carrying them is the same page under a different label — and a real page
#: found that way was queued from `…&partner_category=index&partner_medium=web`
#: in a replay of this instance's own stored pages.
_TRACKING_QUERY_KEYS = frozenset("""
utm_source utm_medium utm_campaign utm_term utm_content utm_id utm_name
gclid fbclid msclkid mc_cid mc_eid igshid ref ref_src referrer source
campaign campaign_id ad_id adid aff aff_id affid partner partner_id
partner_category partner_medium partner_network cid icid
""".split())

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: object) -> set[str]:
    return {w for w in _WORD.findall(str(text or "").lower()) if len(w) >= 4}


def plan_terms(plan: dict | None) -> set[str]:
    """The vocabulary a plan is looking for, from the plan itself.

    Taken from the entity, the dedupe keys, the field names and the search
    queries — the things the user typed or the planner inferred. A link whose
    anchor or slug shares a word with those is more likely to hold the records.
    """
    plan = plan or {}
    out: set[str] = set()
    for key in ("entity", "goal"):
        out |= _terms(plan.get(key))
    for k in (plan.get("dedupe_keys") or []):
        out |= _terms(k)
    for f in (plan.get("fields") or []):
        if isinstance(f, dict):
            out |= _terms(f.get("name"))
    for q in (plan.get("search_queries") or []):
        out |= _terms(q)
    return out


def score_link(link: str, anchor: str = "", plan: dict | None = None,
               terms: set[str] | None = None) -> float:
    """How likely this link is to hold records for this plan. Higher is better.

    This reorders and trims the crawl queue. It never admits anything
    `traversal_allowed` has already refused, and it never bypasses a cap — it
    only decides which allowed links are worth a page budget when there are more
    candidates than budget.

    The sign convention is deliberate: a link can score *negative*, and those
    are dropped first rather than merely last, because a page budget spent on
    `/privacy-policy` is a page budget not spent on a company.
    """
    if not link:
        return -99.0
    # Scored on the canonical resource, not on the label it was found under.
    # The pipeline already canonicalises before scoring, but this function is
    # callable on its own and must judge the same document either way.
    try:
        link = canonical_url(link)
    except Exception:
        return -99.0
    try:
        p = urlparse(link)
    except Exception:
        return -99.0
    # Must be absolute. `ht tp://%%%` parses with an empty scheme and a path of
    # `ht tp://%%%`, so it collected the anchor and segment bonuses and scored
    # +4.0 — a malformed href was ranked above a real company page. Every link
    # that reaches here has been through urljoin, so a missing netloc means the
    # href was junk.
    if not p.netloc:
        return -50.0
    if p.scheme and p.scheme not in ("http", "https"):
        return -50.0
    raw_path = (p.path or "").strip("/")
    # Compare on the stem: `sitemap.xml` is the same furniture as `sitemap`,
    # and the extension denylist in `traversal_allowed` never sees it either
    # because it only matches a known-bad extension list.
    segs = [re.sub(r"\.[a-z0-9]{1,5}$", "", s, flags=re.I) or s
            for s in raw_path.lower().split("/") if s]
    words = terms if terms is not None else plan_terms(plan)
    score = 0.0

    # --- negatives: things that are page furniture, not records ---------------
    if not segs:
        score -= 2.0
    if any(s in _NAV_SEGMENTS for s in segs[:2]):
        score -= 5.0
    # parse_qsl yields (key, value) PAIRS. Reading them as bare keys raised
    # AttributeError, and the broad except below turned that into "no query
    # penalty at all" — so `?page=2` scored positive and pagination was
    # followed as if it were a new resource. Narrowed, and the unpack is
    # explicit, because a scoring bug that silently disables a penalty is worse
    # than one that crashes.
    try:
        qs = {k.lower() for k, _v in parse_qsl(p.query or "", keep_blank_values=False)}
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"unparseable query in {link!r}: {exc}") from exc
    if qs & _NAV_QUERY_KEYS:
        score -= 4.0
    tracking = len(qs & _TRACKING_QUERY_KEYS)
    if tracking:
        score -= min(4.0, 2.0 + 0.5 * tracking)

    # --- positives: things that look like the thing being asked for ----------
    if words:
        hits = _terms(anchor) & words
        if hits:
            score += 2.0 + 0.5 * (len(hits) - 1)
        # Only the LAST segment counts. `/companies/northwind` was earning a
        # slug bonus for "companies" — its parent — which is the least
        # informative word in the path and the reason a section index used to
        # outrank the detail pages it links to.
        if segs and _terms(segs[-1]) & words:
            score += 1.0
    if anchor:
        score += 0.5
    if len(segs) == 2:
        score += 0.5           # /companies/acme — the shape a detail page takes
    elif len(segs) >= 4:
        score -= 1.0           # deep, usually a permalink past the data

    return score


def traversal_allowed(link: str, plan: dict) -> bool:
    """Pure gate for depth+1 traversal links: extension denylist + domain allowlist."""
    try:
        path = urlparse(link).path or ""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    except Exception:
        return False
    if ext in _IGNORED_TRAVERSAL_EXTS:
        return False
    try:
        host = (urlparse(link).hostname or "").lower()
    except Exception:
        return False
    return _domain_allowed(host, _plan_domains(plan or {}))


def _media_counts(page: dict, url: str) -> dict:
    """Persisted media inventory for a settled page (counts only, never blobs)."""
    links = page.get("links")
    if not isinstance(links, list):
        try:
            links = fetcher.extract_links(page.get("html", ""), url)
        except Exception:
            links = []
    return {"images": len(page.get("images", []) or []),
            "videos": len(page.get("videos", []) or []),
            "links": len(links or [])}


def _pick_index(queue: list[tuple[dict, int]], settled: dict[str, int]) -> int:
    """Host-round-robin pick: least-settled host first, earliest queued on ties.

    Sequential processing still benefits: same-host URLs stop bunching up, so
    one slow domain cannot stall the crawl behind its own backlog.
    """
    best, best_key = 0, None
    for i, (item, _depth) in enumerate(queue):
        key = (settled.get(_domain(item["url"]), 0), i)
        if best_key is None or key < best_key:
            best, best_key = i, key
    return best


async def _one(url: str, route: str, fetch_fn: Any, sem: asyncio.Semaphore) -> dict:
    """Waterfall with thinness-escalation (Phase 6).

    A static rung that returns HTTP-200-but-almost-empty WITH scripts is a JS
    shell, not a page — when a renderer remains untried, continue to it
    instead of settling. Scriptless thin pages (example.com) return at once.
    When every rung is thin, the longest page wins (best effort, labeled).
    """
    methods = _methods_for(route, url)
    thin_at = settings.FETCH_THIN_CHARS
    last_err: Exception | None = None
    best: dict | None = None
    best_len = -1
    async with sem:
        for attempt, method in enumerate(methods):
            remaining = methods[attempt + 1:]
            try:
                page = await fetch_fn(url, method)
                page["route"] = route
                blob = (page.get("markdown") or page.get("html") or page.get("text") or "")
                page["content_hash"] = hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()
                if page.get("skipped"):
                    return page
                # Thinness is measured on CLEAN text (what the LLM would read),
                # never on raw HTML — a JS shell's raw HTML is big, its text tiny.
                clean = page.get("markdown") or await asyncio.to_thread(
                    reducer_svc.reduce_html, page.get("html", ""))
                html = page.get("html", "")
                shell = politeness_svc.looks_js_shell(html, len(clean), thin_at)
                hollow = politeness_svc.has_hollow_code(html)
                sparse = politeness_svc.low_density_shell(html, len(clean),
                                                          len(page.get("html", "")))
                if "crawl4ai" not in remaining or not (shell or hollow or sparse):
                    return page
                if len(clean) > best_len:
                    best, best_len = page, len(clean)
                politeness_svc.stats.note_retry(f"thin-escalate:{method}")
                continue  # JS shell / hollow code + renderer untried: escalate
            except AppError as e:
                last_err = e
                if e.code == "E_PROVIDER_FATAL":
                    if attempt == 0 and len(methods) > 1:
                        continue  # one alternate-method attempt (never bypass)
                    break
                if politeness_svc.strategy(e) == "escalate":
                    politeness_svc.stats.note_retry(f"escalate:{method}")
                    continue  # client-shaped failure: alternate now, no backoff
                if attempt < settings.RUN_RETRY_COUNT:
                    politeness_svc.stats.note_retry(method)
                    await fetcher.backoff(attempt)  # transient: backoff then alternate
                    continue
                break
            except Exception as e:  # noqa: BLE001 (provider surface)
                last_err = e
                if attempt < settings.RUN_RETRY_COUNT:
                    politeness_svc.stats.note_retry(method)
                    await fetcher.backoff(attempt)
                    continue
                break
    if best is not None:
        return best
    raise last_err if last_err else RuntimeError("fetch failed")


def _child_links(page: dict) -> list[str]:
    """Same-host child URLs of a settled page. URLs only.

    Kept for callers that do not need anchor text. `_child_links_with_text` is
    what the queue uses, because scoring a link without its anchor throws away
    the only signal that distinguishes a listing from a login page.
    """
    return [u for u, _t in _child_links_with_text(page)]


def _child_links_with_text(page: dict) -> list[tuple[str, str]]:
    """Same-host child URLs of a settled page, paired with anchor text.

    From whichever rung produced it. The static rungs return `html`; the
    rendered rungs return `markdown` plus a parsed `links` list and never an
    `html` key. Reading only `html` therefore found children on plain pages and
    none at all on rendered ones, so a JS-heavy seed never expanded at all.
    Both are honoured now, in that order, with the renderer's own parsed links
    preferred because they are post-JS.

    A rendered page's parsed `links` carries no anchor text, so those come back
    with an empty string and are scored on their URL alone. That is a worse
    score, not a wrong one: an informative URL still scores, and an uninformative
    one is pushed behind links that say what they are.
    """
    parsed = page.get("links")
    if isinstance(parsed, list) and parsed:
        out: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for x in parsed:
            if isinstance(x, (list, tuple)) and len(x) >= 2:
                u, t = str(x[0]), str(x[1])
            elif isinstance(x, (str, bytes)):
                u, t = (x.decode() if isinstance(x, bytes) else x), ""
            else:
                continue
            # Deduped here, not only in the static extractor. The rendered rungs
            # hand over a flat list of links that routinely repeats the same URL
            # once per navigation bar that contains it, and each repeat consumed
            # a page budget slot: a replay of six real pages queued the same
            # `dezerv?partner` link several times over.
            if u in seen_urls:
                continue
            seen_urls.add(u)
            out.append((u, t))
        if out:
            return out
    html = page.get("html") or page.get("rendered_html") or ""
    if html:
        try:
            return list(fetcher.extract_links_with_text(html, page.get("url", "")))
        except Exception:  # noqa: BLE001 (malformed markup must not kill the crawl)
            return []
    return []


def _rank_children(pairs: list[tuple[str, str]], plan: dict, terms: set[str],
                   seen: set[str], depth: int, budget: int,
                   per_parent: int) -> list[str]:
    """Canonicalise, gate, score, trim, and return the URLs worth a fetch.

    One function for both queue sites — the reused-page path and the fetched
    path — so the two cannot drift apart. They are the same decision about the
    same links, and having them written twice is how a fix lands in one and not
    the other.
    """
    scored: list[tuple[float, str]] = []
    added: set[str] = set()
    for raw, anchor in pairs:
        link = canonical_url(raw)
        # Canonical before the seen check: a client-side route such as
        # `/#/login` is the same document as the page we are on, and following
        # it costs a fetch and a budget slot for a byte-identical response.
        if not link or link in seen or link in added:
            continue
        if not traversal_allowed(link, plan):
            continue
        added.add(link)
        scored.append((score_link(link, anchor, plan, terms), link))
    # Best first, then by URL so the order is stable for a given page.
    scored.sort(key=lambda s: (-s[0], s[1]))
    out = [link for score, link in scored if score >= 0]
    return out[:per_parent]


async def fetch_all(urls: list[dict], fetch_fn: Any, emit: Any, persist_source: Any,
                    plan: dict | None = None,
                    persist_page: Any = None,
                    reuse: Any = None) -> tuple[list[dict], dict]:
    """Seeds (depth 0) then breadth-first same-host traversal within caps.

    Returns (pages, {attempted, successful, failed, skipped, reused}). Every
    settled URL is persisted exactly once (progressive; idempotent on retry via
    run_id+url).

    `persist_page` stores the page body and returns its id. That id is what makes
    the rest of the pipeline checkable: previously a page was fetched, reduced in
    RAM, extracted from and thrown away, so every evidence quote and offset pointed
    at text that no longer existed. When it is not supplied (unit tests, the /api/map
    probe) the run still works, it just carries no re-verifiable evidence.

    `reuse` is a coroutine `(url) -> stored page dict | None`, injected rather
    than imported so this stays testable with a fake fetch and so the repository
    stays out of the crawler. When it returns a page, the URL is not fetched
    again: the stored evidence is used, the source row records where it came from
    and when it was actually retrieved, and a `source.reused` event is emitted.

    Reuse is a cost decision with a correctness cost attached — a URL
    fingerprint cannot tell that the page changed upstream — so it is
    `source.reused` rather than a silent `source.fetched`, carries the original
    `retrieved_at`, and can be switched off with `plan["reuse_stored_pages"]`.
    """
    plan = plan or {}
    reuse_enabled = bool(plan.get("reuse_stored_pages", True)) and reuse is not None
    # Computed once for the whole crawl. `plan_terms` walks the schema and the
    # search queries, and doing that per link per page turned a cheap comparison
    # into a repeated parse of the plan.
    terms = plan_terms(plan)
    # How many children one page may contribute. Generous on purpose: scoring
    # orders and trims, it must not starve a page that legitimately has many
    # real candidates, and a listing page can link to hundreds of companies.
    per_parent_cap = max(2, min(int((plan.get("traversal", {}) or {}).get(
        "max_children_per_page", 8)), 40))
    traversal = plan.get("traversal", {}) or {}
    per_domain_cap = max(1, min(int(traversal.get("max_pages_per_domain", 3)),
                                settings.RUN_MAX_DOMAIN_PAGES))
    max_depth = max(1, min(settings.RUN_MAX_DEPTH, 5))
    budget = min(int(plan.get("max_pages", settings.RUN_MAX_PAGES)), settings.RUN_MAX_PAGES)

    sem = asyncio.Semaphore(4)
    pages: list[dict] = []
    ok = fail = skipped = 0
    reused = 0
    seen: set[str] = set()
    domain_count: dict[str, int] = {}
    # Seeds are canonicalised on entry so a seeded fragment variant cannot
    # spend a slot the base URL already occupies.
    queue: list[tuple[dict, int]] = [
        ({**u, "url": canonical_url(u.get("url", ""))}, 0)
        for u in urls if canonical_url(u.get("url", ""))
    ]

    async def _settle(item: dict, depth: int) -> None:
        nonlocal ok, fail, skipped, reused
        # Canonical before anything keys off the URL: the seen set, the
        # per-domain cap, the stored row and the crawl all agree on one
        # identity per resource.
        url = canonical_url(item.get("url", ""))
        if not url:
            return
        dom = _domain(url)
        if depth > 0 and domain_count.get(dom, 0) >= per_domain_cap:
            return  # traversal cap (seeds always allowed); uncounted, unpersisted
        # Reserve the slot BEFORE the await, not after.
        #
        # The cap used to be read above and incremented below `await _one()`.
        # _one() yields control - to the network, and now to the HTML parser -
        # so every task already in flight passed the check against the same
        # stale count, then all incremented. The cap was a suggestion: a run
        # with a domain cap of 10 crawled 13. It had gone unnoticed because a
        # fake fetch that never suspends cannot expose it; a real one always
        # does. A failure or a skip gives the slot back below.
        if dom:
            domain_count[dom] = domain_count.get(dom, 0) + 1
        route = item.get("route") or triage_source(url)

        # Reuse before the fetch, not after: the point is to not spend the
        # request. Asked after `_one` returns, the money is already gone and the
        # check would only be a label on work that was paid for twice.
        if reuse_enabled:
            try:
                stored = await reuse(url)
            except Exception:  # noqa: BLE001
                # A reuse lookup that fails must fall through to a real fetch.
                # Silently "reusing" nothing would drop the URL and shrink the
                # dataset for a reason that is not the data's fault.
                stored = None
            if stored and stored.get("markdown"):
                reused += 1
                ok += 1
                # A page shaped like one `_one` returns, so extraction and
                # validation cannot tell it was reused. `page_id` points at the
                # stored row, so every quote still resolves against real text.
                page = {
                    "url": stored.get("url") or url,
                    "final_url": stored.get("final_url", ""),
                    "title": stored.get("title") or item.get("title", ""),
                    "html": stored.get("raw_html", "") or "",
                    "markdown": stored.get("markdown", ""),
                    "content_hash": stored.get("content_hash", ""),
                    "method": stored.get("method", "") or "reused",
                    "page_id": str(stored.get("id", "")),
                    "depth": depth,
                    "parent_url": item.get("parent_url", "") or "",
                    "reused_from_run_id": str(stored.get("run_id", "")),
                    "retrieved_at": stored.get("retrieved_at"),
                }
                pages.append(page)
                await persist_source({
                    "url": url, "title": page["title"], "status": "reused",
                    "method": page["method"], "error": "",
                    "content_hash": page["content_hash"],
                    "snapshot_chars": len(page["markdown"]),
                    "media": _media_counts(page, url),
                    "reused_from_run_id": page["reused_from_run_id"],
                    "reused_page_id": page["page_id"],
                    "retrieved_at": page["retrieved_at"],
                    "fingerprint": politeness_svc.fingerprint("GET", url)})
                if emit is not None:
                    await emit({"type": "source.reused", "run_id": "",
                                "message": f"reused stored page from an earlier run: {url[:90]}",
                                "stage": RunStage.FETCHING,
                                "data": {"url": url,
                                         "page_id": page["page_id"],
                                         "reused_from_run_id": page["reused_from_run_id"],
                                         "retrieved_at": page["retrieved_at"]}})
                # Traversal still proceeds: the stored page carries its links,
                # and a reused page that stopped discovery would quietly shrink
                # every subsequent run.
                if depth < max_depth:
                    for link in _rank_children(
                            _child_links_with_text(page), plan, terms, seen, depth,
                            budget, per_parent_cap):
                        queue.append(({"url": link, "title": "",
                                       "parent_url": url}, depth + 1))
                return

        try:
            page = await _one(url, route, fetch_fn or _default_fetch, sem)
        except Exception as e:  # noqa: BLE001 (skip-and-continue by design)
            fail += 1
            if dom:
                domain_count[dom] = max(0, domain_count.get(dom, 0) - 1)
            err = getattr(e, "message", str(e))[:300]
            await persist_source({"url": url, "title": item.get("title", ""),
                                  "status": "failed", "method": "", "error": err,
                                  "content_hash": "",
                                  "media": {"images": 0, "videos": 0, "links": 0},
                                  "fingerprint": politeness_svc.fingerprint("GET", url)})
            return
        reason = page.get("skipped", "")
        if reason:
            skipped += 1
            if dom:
                domain_count[dom] = max(0, domain_count.get(dom, 0) - 1)
            await persist_source({"url": url, "title": page.get("title", ""),
                                  "status": "skipped", "method": page.get("method", ""),
                                  "error": reason, "content_hash": "",
                                  "media": {"images": 0, "videos": 0, "links": 0},
                                  "fingerprint": politeness_svc.fingerprint("GET", url)})
            return
        ok += 1
        page["title"] = page.get("title") or item.get("title", "")
        page["depth"] = depth
        page["parent_url"] = item.get("parent_url", "") or ""
        snapshot_chars = len(page.get("rendered_html", "") or page.get("html", "")
                             or page.get("markdown", "") or "")

        # Store the evidence, and keep the id on the page dict so extraction and
        # validation can cite it. markdown is what the LLM actually saw, so
        # quotes resolve against it; raw_html is the snapshot for re-checking
        # content_hash. A storage failure here is reported, never silent: a run
        # whose evidence could not be saved is not a trustworthy run.
        if persist_page is not None:
            try:
                pid = await persist_page({
                    "url": url, "final_url": page.get("final_url", ""),
                    "parent_url": page["parent_url"], "depth": depth,
                    "method": page.get("method", ""), "status": "ok", "error": "",
                    "content_hash": page.get("content_hash", ""),
                    # Shared with the extractor, so a verified quote always
                    # resolves against the stored copy.
                    "markdown": await asyncio.to_thread(reducer_svc.page_evidence_text, page),
                    "raw_html": page.get("rendered_html", "") or page.get("html", ""),
                    "snapshot_chars": snapshot_chars})
                if pid:
                    page["page_id"] = pid
            except Exception as e:  # noqa: BLE001
                fail += 1
                ok -= 1
                await persist_source({"url": url, "title": page["title"],
                                      "status": "failed", "method": page.get("method", ""),
                                      "error": f"evidence not stored: {str(e)[:200]}",
                                      "content_hash": page.get("content_hash", ""),
                                      "snapshot_chars": snapshot_chars,
                                      "media": _media_counts(page, url),
                                      "fingerprint": politeness_svc.fingerprint("GET", url)})
                return
        pages.append(page)
        await persist_source({"url": url, "title": page["title"], "status": "ok",
                              "method": page.get("method", ""), "error": "",
                              "content_hash": page.get("content_hash", ""),
                              "snapshot_chars": snapshot_chars,
                              "media": _media_counts(page, url),
                              "fingerprint": politeness_svc.fingerprint("GET", url)})
        await emit_source(emit, url, True)
        # Traversal reads `html`, which the rendered rungs never return: a
        # Crawl4AI page has markdown and rendered_html only. Gating on `html`
        # therefore dead-ended subpage discovery for exactly the JS-heavy pages
        # that need it most, while their parsed links sat unused in the dict.
        # `depth < max_depth`, not `depth + 1 < max_depth`: the old form made
        # RUN_MAX_DEPTH=2 mean ONE hop, because a child at depth 1 evaluated
        # 2 < 2 and stopped. The setting now means what it says - that many
        # levels below the seeds.
        if depth < max_depth:
            for link in _rank_children(
                    _child_links_with_text(page), plan, terms, seen, depth,
                    budget, per_parent_cap):
                queue.append(({"url": link, "title": "",
                               "parent_url": url}, depth + 1))
                if len(queue) >= budget + 100:
                    # The queue cannot outgrow the budget by more than this
                    # headroom, or a page with thousands of links would let the
                    # workers keep pulling while the budget is already spent.
                    break

    settled: dict[str, int] = {}
    in_flight = 0

    async def _worker() -> None:
        nonlocal in_flight
        while True:
            # Sync pick section (no awaits): claim is race-free on one loop.
            # Duplicates are skipped WITHOUT counting; budget counts every
            # claim, so settled + in-flight never exceeds it.
            pick: tuple[dict, int] | None = None
            if queue and ok + fail + skipped + in_flight < budget:
                item, depth = queue.pop(_pick_index(queue, settled))
                # Claimed on the canonical form, matching the key traversal
                # checks and the key `_settle` persists under. Keying the
                # claim on the raw URL would let a fragment variant through
                # both checks and spend a slot on the same document.
                key = canonical_url(item["url"])
                if key and key not in seen:
                    seen.add(key)
                    in_flight += 1
                    pick = (item, depth)
            if pick is None:
                if in_flight == 0:
                    return  # queue drained and nothing running: done
                await asyncio.sleep(0.02)  # traversal may still append
                continue
            item, depth = pick
            try:
                await _settle(item, depth)
            finally:
                in_flight -= 1
                host = _domain(item["url"])
                settled[host] = settled.get(host, 0) + 1

    await asyncio.gather(*(_worker() for _ in range(_WORKERS)))

    # `fetched` is reported next to `reused` because the pair is the whole
    # point of reuse. `successful` alone could not show it: a reused page
    # counts as a success, so a run that fetched nothing and reused
    # everything looked identical to one that did the work.
    counts = {"attempted": ok + fail + skipped, "successful": ok,
              "failed": fail, "skipped": skipped, "reused": reused,
              "fetched": max(0, ok - reused)}
    return pages, counts


async def emit_source(emit: Any, url: str, ok_flag: bool) -> None:
    import datetime
    await emit({"type": "source.fetched", "run_id": "",
                "stage": "FETCHING", "message": f"{'Fetched' if ok_flag else 'Skipped'} {url[:80]}",
                "progress": 40, "timestamp": datetime.datetime.utcnow().isoformat() + "Z"})


async def _default_fetch(url: str, method: str) -> dict[str, Any]:
    if method == "crawl4ai":
        return await fetcher.crawl4ai_fetch(url)
    if method == "impersonate":
        return await impersonation_svc.impersonate_fetch(url)
    if method == "jina":
        return await fetcher.jina_fetch(url)
    return await fetcher.http_fetch(url)
