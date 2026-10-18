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
import datetime
import hashlib
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from app.core.config import settings
from app.core.constants import RunStage
from app.core.errors import AppError
from app.core.logging import log
from app.providers.crawl import fetcher
from app.services import impersonation as impersonation_svc
from app.services import politeness as politeness_svc
from app.services import reducer as reducer_svc
from app.services.source_router import triage_source

_ORDER = {"web:http": ("http", "crawl4ai"),
          "web:crawl4ai": ("crawl4ai", "http"),
          # `document:docling` prefers the document rung first, but only when
          # the flag is on; otherwise it keeps the old http->crawl4ai order so
          # a PDF seed degrades to the honest `non-html` skip rather than to a
          # rung that cannot run.
          "document:docling": ("http", "crawl4ai"),
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

    Two flag-gated rungs complete the order table: `docling` leads document
    routes only when FEATURE_DOCLING is on (and installed; otherwise the rung
    itself refuses with a named error), and `jina` trails web routes only when
    FEATURE_JINA is on. Both default off, so default behavior is unchanged.
    """
    if route == "document:docling" and getattr(settings, "FEATURE_DOCLING", False):
        methods = ["docling", "http", "crawl4ai"]
    else:
        methods = list(_ORDER.get(route, ("http", "crawl4ai")))
    if ("http" in methods and "impersonate" not in methods
            and impersonation_svc.is_allowlisted(url)):
        methods.insert(methods.index("http") + 1, "impersonate")
    if (getattr(settings, "FEATURE_JINA", False) and route.startswith("web:")
            and "jina" not in methods):
        methods.append("jina")
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
#: pages that differ only by `?sort=name` or `?filter=active` are one resource
#: shown two ways, and `canonical_url` strips fragments but not these.
_NAV_QUERY_KEYS = frozenset({"sort", "orderby", "order",
                             "filter", "q", "search", "ref", "from", "view"})

#: Offset-style pagination keys. These were in `_NAV_QUERY_KEYS` and that made
#: pagination structurally unreachable: `?page=2` scored `-4.0` there, plus the
#: usual penalties, landing below the `score >= 0` cut in `_rank_children`. A
#: listing's second page is a *different set of records*, not a re-rendering of
#: the first, so dropping it is exactly backwards — it is where the rest of the
#: results are. They are no longer penalised as furniture; `rel="next"` and
#: sequential offsets are positively rewarded in `score_link` instead.
_PAGINATION_QUERY_KEYS = frozenset({"page", "offset", "start", "p", "pg"})

#: Path segments that turn a following ordinal into a page number, for the
#: path-style form (`/page/2`, `/p/12`) that carries no query string at all.
_PAGINATION_PATH_SEGMENTS = frozenset({"page", "pages", "p", "pg"})

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

#: The share of a run's page budget that may be spent on seeds.
#:
#: The remaining `1 - SEED_BUDGET_SHARE` is reserved for children, which is the
#: only way to guarantee `max_depth` means anything. Discovery routinely offers
#: as many seeds as there are page slots, so without a reserve the budget is gone
#: before traversal begins and the crawl is flat — every seed fetched, no depth.
SEED_BUDGET_SHARE = 0.4


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


def _query_keys(link: str) -> set[str]:
    """Lowercased query keys of a URL. Empty when it cannot be parsed."""
    try:
        return {k.lower() for k, _v in
                parse_qsl(urlparse(link).query or "", keep_blank_values=False)}
    except (ValueError, TypeError):
        return set()


def _is_pagination(link: str, anchor: str, query_keys: set[str]) -> bool:
    """Whether this link continues a listing rather than leaving it.

    Three shapes cover essentially every real listing:

    * `?page=2`, `?offset=40` — the key is in `_PAGINATION_QUERY_KEYS`;
    * `/page/2`, `/companies?page=2` — a path segment that is a bare ordinal;
    * anchor text saying so — `Next`, `Older posts`, `Next page`, and the `next`
      marker this codebase hands back for `<link rel="next">`.

    The anchor alone is trusted only for unambiguous continuations. `More` and
    `View all` are deliberately excluded: they usually mean a different page
    (`/archive`), not page two of this one.
    """
    if query_keys & _PAGINATION_QUERY_KEYS:
        return True
    text = re.sub(r"\s+", " ", anchor or "").strip().lower()
    if re.fullmatch(r"(next( page)?|older( posts?)?|newer( posts?)?"
                    r"|page \d+|next \d+|>\s*|»)", text):
        return True
    try:
        segs = [s.lower() for s in (urlparse(link).path or "").strip("/").split("/") if s]
    except ValueError:
        return False
    # A trailing bare ordinal: `/page/2`, `/companies/3`, `/p/12`.
    return bool(segs) and len(segs) <= 2 and segs[-1].isdigit() and \
        segs[-2] in _PAGINATION_PATH_SEGMENTS


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

    # --- pagination: a distinct set of records, so worth a fetch -------------
    # These keys used to sit in `_NAV_QUERY_KEYS` for -4.0, which put every
    # `?page=2` below the `score >= 0` cut. A listing's later pages hold the
    # records the first page did not, so the penalty was spending the budget on
    # the seed and discarding everything behind it.
    #
    # Path-style pagination (`/page/2/`, used by quotes.toscrape.com) carries no
    # query key at all, so the anchor is the only signal. Both shapes are scored
    # here, and `_rank_children` additionally guarantees the best one survives
    # the per-parent trim — on a listing page of author links, `/page/2/` ties
    # with every author and lost all eight slots.
    if _is_pagination(link, anchor, qs):
        score += 3.0

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


def _status_for_error(err: object) -> str:
    """`timed_out` for a timeout, `failed` for anything else."""
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return "timed_out"
    code = str(getattr(err, "code", "") or "")
    if code == "E_RENDER_FAILED" or "timeout" in str(
            getattr(err, "message", err) or "").lower():
        return "timed_out"
    return "failed"


def _render_code(err: object, method: str) -> str:
    """The taxonomy code for a failed attempt on `method`.

    Prefers the code the fetch layer already attached, so the browser rung's
    classification is not re-derived here from a truncated message. Non-render
    rungs are honestly `not_applicable` rather than being folded into a render
    code, because "http_error" and "browser_crash" have different causes and
    merging them would make the counts unreadable.
    """
    if method != "crawl4ai":
        return "not_applicable"
    details = getattr(err, "details", None)
    if isinstance(details, dict):
        code = str(details.get("render_error_code") or "")
        if code:
            return code
    return fetcher.classify_render_error(err)


def _note_attempt(attempts: list[dict], method: str, outcome: str,
                  code: str = "") -> None:
    """One entry in the attempt history for this URL."""
    attempts.append({"method": method, "outcome": outcome,
                     "error_code": code or ""})


def _apply_outcome(page: dict, *, attempts: list[dict], fallback_from: object = None,
                   final_source: str = "http") -> dict:
    """Stamp the fetch outcome onto a page dict.

    Six keys, and each answers a question the run used to be unable to answer
    about any individual page:

      fetch_attempts        every rung tried, in order, with its outcome
      render_attempted      was a browser opened at all
      render_status         not_needed / succeeded / failed / timed_out
      render_error_code     which of the closed taxonomy codes, when it failed
      fallback_used         did we serve something other than what we fetched
      final_content_source  rendered | http | none

    The one that mattered: a Chromium death on any page whose HTTP version
    exceeded FETCH_THIN_CHARS used to return the HTTP page as an ordinary
    success, with `method: "http"`, no error and no event. Measured, that turned
    17635 characters of rendered content into 7885 characters of unrendered HTML
    with nothing in the record to say so. The render failure was erased, not
    merely unreported.
    """
    render = [a for a in attempts if a["method"] == "crawl4ai"]
    # The rung's FINAL outcome, not whether anything along the way failed. A
    # retry that succeeds is a successful render; reading "did any attempt fail"
    # instead reported it as failed, which would make the run's render success
    # rate disagree with the pages it actually got. The earlier failures are not
    # lost -- they are in `fetch_attempts`, which is what makes a flaky renderer
    # visible as flaky rather than as broken.
    rendered_ok = bool(render) and render[-1]["outcome"] == "ok"
    if not render:
        status = "not_needed"
    else:
        status = "succeeded" if rendered_ok else "failed"
    page["fetch_attempts"] = attempts
    page["render_attempted"] = bool(render)
    page["render_status"] = status
    page["render_error_code"] = (
        "" if rendered_ok or not render
        else next((a["error_code"] for a in reversed(render)
                   if a["outcome"] == "failed"), ""))
    # A fallback means we did not get the rendered page. Not "a render was
    # tried and something went wrong along the way" -- those are different
    # claims, and conflating them would flag every retried-then-succeeded page.
    page["fallback_used"] = bool(render) and not rendered_ok and final_source != "rendered"
    page["final_content_source"] = final_source
    return page


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
    # Every rung this URL tried, in order. The point is that a page returned after
    # a render failure must be distinguishable from one that was simply read
    # over HTTP -- which, before this, it was not.
    attempts: list[dict] = []
    # RUN_RETRY_COUNT is attempts PER RUNG. It was compared against `attempt`,
    # which was `enumerate(methods)` -- the position in the waterfall, not a
    # retry counter -- so it never retried anything. `0 < 1` meant "move to the
    # next rung"; `1 < 1` meant give up; and raising it to 3 changed nothing,
    # because the loop had already ended. Every rung got exactly one shot, ever.
    #
    # That is not cosmetic for the browser rung. On the default `web:http` route
    # the order is http -> crawl4ai, so Chromium was index 1: one attempt, no
    # backoff, no second try. A transient Playwright driver disconnect was
    # therefore final for that page, which is the class of failure behind the
    # unexplained render losses in docs/39-REUSE-VERIFICATION.md. The default
    # stays 1, so nothing changes until it is deliberately raised -- but when it
    # is raised it now means what its name says.
    attempts_per_rung = max(1, int(settings.RUN_RETRY_COUNT))
    stop = False
    async with sem:
        for index, method in enumerate(methods):
            if stop:
                break
            remaining = methods[index + 1:]
            for trial in range(attempts_per_rung):
                try:
                    page = await fetch_fn(url, method)
                    page["route"] = route
                    _note_attempt(attempts, method, "ok")
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
                        # Either this page is genuinely readable, or it is the last
                        # rung and still looks like a shell. Those are opposites, and
                        # they used to share one `return page` — which handed an
                        # unreadable shell straight to the extractor on the final
                        # attempt, so `NA` values got stored as real records.
                        if not (shell or hollow or sparse):
                            return _apply_outcome(
                                page, attempts=attempts,
                                final_source=("rendered" if method == "crawl4ai"
                                              else "http"))
                        if len(clean) > best_len:
                            best, best_len = page, len(clean)
                        break  # to the tail, which refuses to call a shell a page
                    if len(clean) > best_len:
                        best, best_len = page, len(clean)
                    politeness_svc.stats.note_retry(f"thin-escalate:{method}")
                    break  # renderer untried: move down the waterfall, not around
                           # it. A shell does not become readable by asking the
                           # same rung twice, and a retry here would spend a page
                           # attempt to learn exactly what we already know.
                except AppError as e:
                    last_err = e
                    _note_attempt(attempts, method, "failed",
                                  _render_code(e, method))
                    if e.code == "E_PROVIDER_FATAL":
                        if index == 0 and len(methods) > 1:
                            break  # one alternate-method attempt (never bypass)
                        stop = True  # fatal mid-waterfall: stop, do not alternate
                        break
                    if politeness_svc.strategy(e) == "escalate":
                        politeness_svc.stats.note_retry(f"escalate:{method}")
                        break  # client-shaped failure: alternate now, no backoff
                    politeness_svc.stats.note_retry(method)
                    if trial < attempts_per_rung - 1:
                        await fetcher.backoff(trial)  # transient: this rung again
                        continue
                    if remaining:
                        # Spent here. Pause before dropping to the next rung --
                        # the old code did this on the way *out* of a rung, and
                        # `test_retry_notes_and_backs_off` pins it, so it is a
                        # deliberate part of the waterfall rather than a retry.
                        await fetcher.backoff(trial)
                    break
                except Exception as e:  # noqa: BLE001 (provider surface)
                    last_err = e
                    _note_attempt(attempts, method, "failed",
                                  _render_code(e, method))
                    politeness_svc.stats.note_retry(method)
                    if trial < attempts_per_rung - 1:
                        await fetcher.backoff(trial)
                        continue
                    if remaining:
                        await fetcher.backoff(trial)
                    break
    if best is not None:
        # A page that stayed a JS shell through every rung is not a page we
        # read — it is a page we failed to render, and returning it as `best`
        # let the extractor mine `NA` out of an empty shell and record those as
        # real records. `/api/intel/ask` already refuses this case; the run
        # pipeline had no equivalent, so a broken renderer produced confident
        # nonsense instead of a visible failure.
        final_html = best.get("html", "")
        final_clean = best.get("markdown") or await asyncio.to_thread(
            reducer_svc.reduce_html, final_html)
        final_len = len(final_clean)
        if final_len < settings.FETCH_THIN_CHARS:
            raise AppError("E_RENDER_FAILED",
                           f"the page is still a JavaScript shell after "
                           f"rendering ({final_len} chars of real text, needs "
                           f"{settings.FETCH_THIN_CHARS}); nothing was readable, "
                           f"so this page is recorded as unrenderable rather "
                           f"than extracted from an empty shell",
                           422, {"url": best.get("url", ""),
                                 "chars": final_len,
                                 "required": settings.FETCH_THIN_CHARS,
                                 "fetch_attempts": attempts})
        # The degraded case, and the reason this block exists at all.
        #
        # `last_err` is not discarded here. It used to be: a Chromium death on
        # any page whose HTTP version exceeded FETCH_THIN_CHARS returned that
        # page as an ordinary success -- `method: "http"`, no error, no event --
        # and the render failure was gone rather than merely unrecorded.
        #
        # Measured, that turned 17635 characters of rendered content into 7885
        # characters of unrendered HTML, a 55% loss with nothing in the dataset
        # to say a render had even been attempted. So the page is still returned
        # -- the HTTP text is better than nothing, and discarding it would throw
        # away a real source -- but it is returned *labelled*, and the reason it
        # is not what was asked for travels with it.
        out = _apply_outcome(best, attempts=attempts, fallback_from=last_err,
                             final_source="http")
        if last_err is not None:
            out["degraded_reason"] = str(getattr(last_err, "message", last_err))[:200]
            out["degraded_code"] = str(getattr(last_err, "code", "")) or ""
        return out
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
            if isinstance(x, dict):
                # What `fetcher._crawl_result_links` actually produces
                # (`fetcher.py:485-487`): `{"url", "text", "internal"}`. This
                # branch did not exist, so the `else: continue` below discarded
                # every link the renderer had already parsed — including the
                # post-JS links no static parse can see. Children survived only
                # by accident, via a second parse of `rendered_html` capped at 50
                # same-host links, so the renderer's 200 collected links bought
                # nothing.
                u, t = str(x.get("url") or ""), str(x.get("text") or "")
            elif isinstance(x, (list, tuple)) and len(x) >= 2:
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
    paged: list[tuple[float, str]] = []
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
        score = score_link(link, anchor, plan, terms)
        scored.append((score, link))
        if _is_pagination(link, anchor, _query_keys(link)):
            paged.append((score, link))
    # Best first, then by URL so the order is stable for a given page.
    scored.sort(key=lambda s: (-s[0], s[1]))
    out = [link for score, link in scored if score >= 0]
    if len(out) > per_parent and paged:
        # A listing's own next page must not be crowded out by the records that
        # page happens to list. On a page of author links, `/page/2/` tied with
        # every author at 1.0 and lost all eight per-parent slots to the
        # alphabetical tiebreak — so the rest of the listing was never fetched.
        # Reserve one slot for the best continuation.
        best_paged = max(paged, key=lambda s: (-s[0], s[1]))[1]
        kept = out[:per_parent - 1]
        if best_paged in out:
            kept.append(best_paged)
            out = kept
        else:
            out = kept
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
    per_domain_cap = max(1, min(int(traversal.get("max_pages_per_domain", 10)),
                                settings.RUN_MAX_DOMAIN_PAGES))
    max_depth = max(1, min(settings.RUN_MAX_DEPTH, 5))
    budget = min(int(plan.get("max_pages", settings.RUN_MAX_PAGES)), settings.RUN_MAX_PAGES)

    # Per-host caps, from the prior where one exists.
    #
    # `per_domain_cap` was a single number for every host, so a site with a
    # 0.95 verified-cell rate and a listicle that has never yielded a record
    # both got the same allowance. `domain_caps` is filled by discovery from
    # `source_reputation`; an absent entry keeps the flat cap, so a cold start
    # behaves exactly as it did before.
    domain_caps: dict[str, int] = {}
    for host, cap in ((plan.get("traversal", {}) or {})
                      .get("domain_caps") or {}).items():
        try:
            domain_caps[str(host).lower()] = max(1, min(int(cap),
                                                      settings.RUN_MAX_DOMAIN_PAGES))
        except (TypeError, ValueError):
            continue

    def _cap_for(host: str) -> int:
        return domain_caps.get(host.lower(), per_domain_cap)

    # Seeds and traversal were drawing on one budget, and seeds won.
    #
    # `discovery` returns up to `max_pages` seeds, so a normal run entered the
    # queue with seeds == budget. `_worker` counts every claim (seeds included)
    # against that same number, so the queue drained without a single child ever
    # being popped: `RUN_MAX_DEPTH` was real, and nothing ever went one level
    # deep. Seeds also bypassed `per_domain_cap` entirely (it is checked at
    # `depth > 0`), so the cap could not compensate either.
    #
    # The fix is to reserve traversal space up front rather than hope the budget
    # has slack. `SEED_BUDGET_SHARE` of the budget may go to seeds; the rest is
    # guaranteed to children.
    seed_cap = max(1, int(budget * SEED_BUDGET_SHARE))

    sem = asyncio.Semaphore(4)
    pages: list[dict] = []
    ok = fail = skipped = 0
    reused = 0
    seen: set[str] = set()
    domain_count: dict[str, int] = {}
    # Truncation causes, counted rather than swallowed. Every one of these used
    # to be a silent `return` or a silent `break`, which is why a crawl that
    # stopped at the seeds looked identical to one that had nothing to follow.
    blocked_by_domain_cap = 0
    blocked_by_budget = 0
    seeds_dropped = 0
    # Did the crawl actually go past its seeds? Tracked explicitly because
    # "successfully fetched N pages" and "fetched N pages, none of them linked
    # from anything it found" are different outcomes with the same counts.
    traversed_pages = 0
    depth_reached = 0
    # Seeds are canonicalised on entry so a seeded fragment variant cannot
    # spend a slot the base URL already occupies.
    _seed_queue: list[tuple[dict, int]] = [
        ({**u, "url": canonical_url(u.get("url", ""))}, 0)
        for u in urls if canonical_url(u.get("url", ""))
    ]
    queue: list[tuple[dict, int]] = _seed_queue[:seed_cap]
    seeds_queued = len(queue)
    seeds_dropped = max(0, len(_seed_queue) - len(queue))
    del _seed_queue
    if seeds_dropped:
        log.warning(
            "seeds truncated to reserve traversal budget",
            extra={"data": {
                "seeds_offered": len(urls), "seeds_queued": len(queue),
                "seeds_dropped": seeds_dropped, "budget": budget,
                "seed_cap": seed_cap, "max_depth": max_depth,
                "reason": (f"a run that spends its whole budget on seeds never "
                           f"traverses; {SEED_BUDGET_SHARE:.0%} of the budget is "
                           f"reserved for children")}})

    # Per-page failures that the optional hooks themselves caused. Kept apart from
    # `fail` because they are our bug, not the site's: the page WAS fetched, so it
    # is in `pages`, and counting it as a fetch failure would understate the yield.
    hook_failures = 0

    async def _record_source(row: dict) -> None:
        """Persist one source row. Never raises.

        `persist_source` is an optional injected hook. Calling it unguarded meant
        a single `TypeError` from a hook — or a `None` passed for one — escaped
        `_settle`, propagated out of `_worker`, and took down the entire crawl,
        discarding every page already fetched. One bad bookkeeping row must cost
        exactly one bookkeeping row.
        """
        nonlocal hook_failures
        if persist_source is None:
            return
        try:
            await persist_source(row)
        except Exception as e:  # noqa: BLE001
            hook_failures += 1
            log.warning("source row not persisted",
                        extra={"data": {"url": str(row.get("url", ""))[:120],
                                        "error": str(e)[:200]}})

    async def _notify(payload: dict) -> None:
        """Emit a progress event. Never raises.

        Same reasoning as `_record_source`: a broken SSE consumer must not be able
        to destroy a crawl.
        """
        if emit is None:
            return
        try:
            await emit(payload)
        except Exception as e:  # noqa: BLE001
            log.debug("emit failed", extra={"data": {"error": str(e)[:160]}})

    async def _settle(item: dict, depth: int) -> None:
        nonlocal ok, fail, skipped, reused, traversed_pages, depth_reached
        # Canonical before anything keys off the URL: the seen set, the
        # per-domain cap, the stored row and the crawl all agree on one
        # identity per resource.
        url = canonical_url(item.get("url", ""))
        if not url:
            return
        dom = _domain(url)
        if depth > 0 and domain_count.get(dom, 0) >= _cap_for(dom):
            # Counted, and reported, rather than dropped in silence. This used to
            # be a bare `return`, so a run stopped at 3 pages of one host looked
            # exactly like a run with no further links to follow — and the cap is
            # the thing most likely to stop recursion into the best host.
            nonlocal blocked_by_domain_cap
            blocked_by_domain_cap += 1
            return
        if depth > 0:
            traversed_pages += 1
            depth_reached = max(depth_reached, depth)
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
                #
                # `html` carries the stored `raw_html` on purpose: this dict has
                # no `links` key, so `_child_links_with_text` falls through to
                # parsing `html`, and a stored page that arrived without it
                # expanded to zero children — silently, one page per run. The
                # repository's `find_page_by_url(url, include_html=True)` does
                # return `raw_html`, so this is belt-and-braces for any store
                # that omits it, and it keeps the reuse branch structurally
                # identical to the fetched branch. `rendered_html` is mirrored
                # too, since `_child_links_with_text` prefers either name.
                page = {
                    "url": stored.get("url") or url,
                    "final_url": stored.get("final_url", ""),
                    "title": stored.get("title") or item.get("title", ""),
                    "html": stored.get("raw_html", "") or "",
                    "rendered_html": stored.get("raw_html", "") or "",
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
                await _record_source({
                    "url": url, "title": page["title"], "status": "reused",
                    "method": page["method"], "error": "",
                    "content_hash": page["content_hash"],
                    "snapshot_chars": len(page["markdown"]),
                    "media": _media_counts(page, url),
                    "reused_from_run_id": page["reused_from_run_id"],
                    "reused_page_id": page["page_id"],
                    "retrieved_at": page["retrieved_at"],
                    "fingerprint": politeness_svc.fingerprint("GET", url)})
                await _notify({"type": "source.reused", "run_id": "",
                               "message": f"reused stored page from an earlier run: {url[:90]}",
                               "stage": RunStage.FETCHING,
                               "data": {"url": url,
                                        "page_id": page["page_id"],
                                        "reused_from_run_id": page["reused_from_run_id"],
                                        "retrieved_at": page["retrieved_at"]}})
                # Traversal proceeds from the stored HTML captured above.
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
            await _record_source({"url": url, "title": item.get("title", ""),
                                  "status": "failed", "method": "", "error": err,
                                  "content_hash": "",
                                  # A failed fetch has no `pages` row at all, so
                                  # the sources row is the only place its cause
                                  # can live. `pages` only records successes.
                                  "render_status": _status_for_error(e),
                                  "render_error_code": _render_code(e, "crawl4ai"),
                                  "fallback_used": False,
                                  "media": {"images": 0, "videos": 0, "links": 0},
                                  "fingerprint": politeness_svc.fingerprint("GET", url)})
            await _notify(_source_failed_event(
                url, err, status="failed",
                error_code=_render_code(e, "crawl4ai")))
            return
        reason = page.get("skipped", "")
        if reason:
            skipped += 1
            if dom:
                domain_count[dom] = max(0, domain_count.get(dom, 0) - 1)
            await _record_source({"url": url, "title": page.get("title", ""),
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

        # A degraded fetch is announced here rather than at the waterfall,
        # because by this point the outcome keys have been applied and this is the
        # one place every successful page passes through. Emitted before the
        # storage attempt so a store failure cannot swallow it.
        if page.get("fallback_used"):
            await _notify(_source_failed_event(
                url, page.get("degraded_reason", "") or "render failed",
                error_code=str(page.get("render_error_code") or ""),
                fallback_used=True))

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
                    "snapshot_chars": snapshot_chars,
                    # What actually happened, per `010_fetch_outcome.sql`. A
                    # stored page that fell back after a render failure has to be
                    # distinguishable from one that was simply read over HTTP,
                    # and nothing above carried that.
                    "fetch_attempts": page.get("fetch_attempts", []),
                    "render_attempted": bool(page.get("render_attempted")),
                    "render_status": page.get("render_status", "not_needed"),
                    "render_error_code": page.get("render_error_code", ""),
                    "fallback_used": bool(page.get("fallback_used")),
                    "final_content_source": page.get("final_content_source", "http"),
                    "degraded_reason": page.get("degraded_reason", ""),
                    # Which reducer produced the `markdown` above. Offsets in
                    # every verified cell index into it, so a future change here
                    # is otherwise undetectable after the fact.
                    "reducer_version": reducer_svc.REDUCER_VERSION})
                if pid:
                    page["page_id"] = pid
            except Exception as e:  # noqa: BLE001
                fail += 1
                ok -= 1
                await _record_source({"url": url, "title": page["title"],
                                      "status": "failed", "method": page.get("method", ""),
                                      "error": f"evidence not stored: {str(e)[:200]}",
                                      "content_hash": page.get("content_hash", ""),
                                      "snapshot_chars": snapshot_chars,
                                      "media": _media_counts(page, url),
                                      "fingerprint": politeness_svc.fingerprint("GET", url)})
                return
        pages.append(page)
        await _record_source({"url": url, "title": page["title"], "status": "ok",
                              "method": page.get("method", ""), "error": "",
                              "content_hash": page.get("content_hash", ""),
                              "snapshot_chars": snapshot_chars,
                              "media": _media_counts(page, url),
                              "fingerprint": politeness_svc.fingerprint("GET", url)})
        await _notify(_source_fetched_event(url))
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
        nonlocal in_flight, fail, blocked_by_budget
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
                # The queue still holds children but the budget is spent. This
                # is the truncation the seed reserve exists to prevent, so it is
                # counted and reported rather than looking like an empty queue.
                if queue and in_flight == 0:
                    blocked_by_budget += len(queue)
                    queue.clear()
                    log.warning(
                        "traversal budget spent with pages still queued",
                        extra={"data": {"queued_untouched": blocked_by_budget,
                                        "budget": budget}})
                if in_flight == 0:
                    return  # queue drained and nothing running: done
                await asyncio.sleep(0.02)  # traversal may still append
                continue
            item, depth = pick
            try:
                await _settle(item, depth)
            except Exception as e:  # noqa: BLE001
                # A worker that dies takes the whole crawl with it: the `gather`
                # below would raise, `fetch_all` would return nothing, and every
                # page already fetched would be lost — over a single bad URL. The
                # page is counted as failed so the budget stays honest and the
                # failure is visible in `counts` rather than silent.
                fail += 1
                log.warning("page settlement failed",
                            extra={"data": {"url": str(item.get("url", ""))[:120],
                                            "error": str(e)[:200]}})
            finally:
                in_flight -= 1
                host = _domain(item["url"])
                settled[host] = settled.get(host, 0) + 1

    # `return_exceptions=True` so one worker raising cannot cancel the others.
    # The `except` in `_worker` is the primary defence; this is the backstop that
    # turns any *future* unguarded path into a logged partial crawl instead of a
    # total loss.
    worker_errors = await asyncio.gather(
        *(_worker() for _ in range(_WORKERS)), return_exceptions=True)
    for err in worker_errors:
        if isinstance(err, BaseException):
            log.error("crawl worker died", extra={"data": {"error": str(err)[:200]}})

    # `fetched` is reported next to `reused` because the pair is the whole
    # point of reuse. `successful` alone could not show it: a reused page
    # counts as a success, so a run that fetched nothing and reused
    # everything looked identical to one that did the work.
    counts = {"attempted": ok + fail + skipped, "successful": ok,
              "failed": fail, "skipped": skipped, "reused": reused,
              "fetched": max(0, ok - reused)}
    if hook_failures:
        # Our hooks failed, not the crawl. Reported separately so a caller never
        # reads this as "N pages failed to fetch".
        counts["hook_failures"] = hook_failures

    # Why the crawl stopped where it did. A `coverage` gap attributed to
    # "crawl-depth" is only actionable if the run can say it never got past the
    # seeds, or hit a per-domain cap, or ran out of budget. All three were
    # previously invisible, so a flat crawl reported the same numbers as a deep
    # one and nothing downstream could tell them apart.
    counts["traversal"] = {
        "seeds_queued": seeds_queued,
        "seeds_dropped": seeds_dropped,
        "traversed_pages": traversed_pages,
        "blocked_by_domain_cap": blocked_by_domain_cap,
        "blocked_by_budget": blocked_by_budget,
        "max_depth": max_depth,
        "depth_reached": depth_reached,
        "per_domain_cap": per_domain_cap,
        "budget": budget,
    }
    if blocked_by_domain_cap or blocked_by_budget or seeds_dropped:
        log.info("crawl stopped early", extra={"data": counts["traversal"]})
    return pages, counts


def _source_fetched_event(url: str, ok_flag: bool = True) -> dict[str, Any]:
    """The `source.fetched` SSE frame, as data rather than an awaitable.

    `data` carries the url and the outcome structurally. It used to be absent and
    the url existed only inside the human-readable `message`, which meant any
    client wanting to build a list of what was actually fetched had to regex a
    sentence.

    A plain function, not an async one, because every emission now goes through
    `_notify`, which owns the try/except. An `async def emit_source` that awaited
    `emit` directly was the other unguarded path that could kill a crawl.
    """
    return {"type": "source.fetched", "run_id": "",
            "stage": "FETCHING",
            "message": f"{'Fetched' if ok_flag else 'Skipped'} {url[:80]}",
            "progress": 40,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "data": {"url": url, "ok": bool(ok_flag)}}


def _source_failed_event(url: str, reason: str, *, status: str = "failed",
                         error_code: str = "", fallback_used: bool = False) -> dict:
    """The `source.failed` SSE frame, for a fetch that did not deliver.

    Failed and skipped sources used to emit nothing at all -- `fetch_all`
    recorded the row and returned. So the run's event history said a page had
    been fetched whenever a page had been fetched, and was silent about every
    one that had not. A client could not list what was tried and refused, and a
    reviewer could not tell "we read 12 pages" from "we read 9 and lost 3".

    `degraded` is the case that mattered and had no representation at all: a
    render that failed and was served over HTTP. The page is real and is kept, so
    it is not a failure -- but it is not what was asked for either, and the
    difference between the two is exactly what a reader of a dataset needs.

    `data` carries the taxonomy code so a UI can group by cause rather than by
    sentence. `status` is one of `failed`, `skipped` or `degraded`.
    """
    if fallback_used:
        label = "Degraded"
        status = "degraded"
    elif status == "skipped":
        label = "Skipped"
    else:
        label = "Failed"
    return {"type": "source.failed", "run_id": "",
            "stage": "FETCHING",
            "message": f"{label} {url[:80]}: {reason[:120]}",
            "progress": 40,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "data": {"url": url, "ok": False, "status": status,
                     "reason": reason[:300], "error_code": error_code,
                     "fallback_used": bool(fallback_used)}}


#: How long each rung's wall-clock is kept for reporting.
#:
#: Bounded on purpose. This is a rolling window over a long crawl, not a
#: per-call log: the useful question is "where does the time go", which needs
#: enough samples to be representative and no more. Unbounded samples on a
#: 500-page crawl would be a memory leak dressed as instrumentation.
_TIMING_WINDOW = 200

#: rung -> samples of {"ms", "ok", "outcome"}. Insertion-ordered; the window is
#: trimmed from the front, which keeps recent behaviour rather than the average
#: of a run that started before the site changed.
_timing: dict[str, list[dict[str, Any]]] = {}
_timing_lock = threading.Lock()


def _note_timing(rung: str, ms: int, ok: bool, outcome: str) -> None:
    with _timing_lock:
        samples = _timing.setdefault(rung, [])
        samples.append({"ms": ms, "ok": bool(ok), "outcome": outcome})
        if len(samples) > _TIMING_WINDOW:
            del samples[: len(samples) - _TIMING_WINDOW]


def fetch_timing() -> dict[str, Any]:
    """Per-rung wall-clock, for diagnosing where a crawl spent its time.

    Reports the shape of the distribution rather than only a mean, because the
    mean hides the thing that matters here: a rung that is usually fast and
    occasionally very slow (browser launch, a PDF conversion) looks identical to
    a uniformly slow rung if you only average it. `p50` and `p90` separate those.

    Failure outcomes are counted by name rather than folded into a boolean,
    because "skipped because robots disallowed" and "threw a timeout" call for
    completely different responses.
    """
    with _timing_lock:
        snapshot = {rung: list(samples) for rung, samples in _timing.items()}
    out: dict[str, Any] = {}
    for rung, samples in snapshot.items():
        times = sorted(s["ms"] for s in samples)
        n = len(times)
        if not n:
            continue
        outcomes: dict[str, int] = {}
        for s in samples:
            outcomes[s["outcome"]] = outcomes.get(s["outcome"], 0) + 1
        out[rung] = {
            "n": n,
            "p50_ms": times[n // 2],
            "p90_ms": times[min(n - 1, int(n * 0.9))],
            "min_ms": times[0],
            "max_ms": times[-1],
            "total_ms": sum(times),
            "failed": sum(1 for s in samples if not s["ok"]),
            "outcomes": dict(sorted(outcomes.items(), key=lambda kv: -kv[1])),
        }
    return out


def reset_fetch_timing() -> None:
    with _timing_lock:
        _timing.clear()


async def _default_fetch(url: str, method: str) -> dict[str, Any]:
    """Dispatch to a fetch rung, timing it.

    Timing lives here rather than inside each rung because this is the one
    place every rung passes through. Instrumenting the five fetch functions
    separately would miss the robots check and the throttle wait, which are part
    of what the crawl actually pays for and were the reason for measuring.
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        if method == "crawl4ai":
            out = await fetcher.crawl4ai_fetch(url)
        elif method == "impersonate":
            out = await impersonation_svc.impersonate_fetch(url)
        elif method == "jina":
            out = await fetcher.jina_fetch(url)
        elif method == "docling":
            out = await fetcher.docling_fetch(url)
        else:
            out = await fetcher.http_fetch(url)
    except BaseException as exc:
        outcome = f"raised:{type(exc).__name__}"
        _note_timing(method, int((time.perf_counter() - started) * 1000),
                     False, outcome)
        raise
    # A rung that returns a skip is not a rung that worked: it reported no
    # content, and counting it as success would make the timing look better
    # than the crawl actually was.
    if out.get("skipped"):
        outcome = str(out.get("skipped"))[:60]
    elif out.get("error"):
        outcome = "error"
    _note_timing(method, int((time.perf_counter() - started) * 1000),
                 not bool(out.get("skipped") or out.get("error")), outcome)
    return out
