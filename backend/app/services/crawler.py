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
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
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
    return (urlparse(url).hostname or "").lower()


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
                clean = page.get("markdown") or reducer_svc.reduce_html(page.get("html", ""))
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


async def fetch_all(urls: list[dict], fetch_fn: Any, emit: Any, persist_source: Any,
                    plan: dict | None = None) -> tuple[list[dict], dict]:
    """Seeds (depth 0) then breadth-first same-host traversal within caps.

    Returns (pages, {attempted, successful, failed, skipped}). Every settled URL is
    persisted exactly once (progressive; idempotent on retry via run_id+url).
    """
    plan = plan or {}
    traversal = plan.get("traversal", {}) or {}
    per_domain_cap = max(1, min(int(traversal.get("max_pages_per_domain", 3)),
                                settings.RUN_MAX_DOMAIN_PAGES))
    max_depth = max(1, min(settings.RUN_MAX_DEPTH, 5))
    budget = min(int(plan.get("max_pages", settings.RUN_MAX_PAGES)), settings.RUN_MAX_PAGES)

    sem = asyncio.Semaphore(4)
    pages: list[dict] = []
    ok = fail = skipped = 0
    seen: set[str] = set()
    domain_count: dict[str, int] = {}
    queue: list[tuple[dict, int]] = [(u, 0) for u in urls]

    async def _settle(item: dict, depth: int) -> None:
        nonlocal ok, fail, skipped
        url = item["url"]
        dom = _domain(url)
        if depth > 0 and domain_count.get(dom, 0) >= per_domain_cap:
            return  # traversal cap (seeds always allowed); uncounted, unpersisted
        route = item.get("route") or triage_source(url)
        try:
            page = await _one(url, route, fetch_fn or _default_fetch, sem)
        except Exception as e:  # noqa: BLE001 (skip-and-continue by design)
            fail += 1
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
            await persist_source({"url": url, "title": page.get("title", ""),
                                  "status": "skipped", "method": page.get("method", ""),
                                  "error": reason, "content_hash": "",
                                  "media": {"images": 0, "videos": 0, "links": 0},
                                  "fingerprint": politeness_svc.fingerprint("GET", url)})
            return
        ok += 1
        domain_count[dom] = domain_count.get(dom, 0) + 1
        page["title"] = page.get("title") or item.get("title", "")
        page["depth"] = depth
        pages.append(page)
        snapshot_chars = len(page.get("rendered_html", "") or page.get("html", "")
                             or page.get("markdown", "") or "")
        await persist_source({"url": url, "title": page["title"], "status": "ok",
                              "method": page.get("method", ""), "error": "",
                              "content_hash": page.get("content_hash", ""),
                              "snapshot_chars": snapshot_chars,
                              "media": _media_counts(page, url),
                              "fingerprint": politeness_svc.fingerprint("GET", url)})
        await emit_source(emit, url, True)
        if depth + 1 < max_depth and page.get("html"):
            for link in fetcher.extract_links(page["html"], url):
                if link not in seen and traversal_allowed(link, plan):
                    queue.append(({"url": link, "title": ""}, depth + 1))
                if len(queue) >= budget + 100:
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
                if item["url"] not in seen:
                    seen.add(item["url"])
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

    counts = {"attempted": ok + fail + skipped, "successful": ok,
              "failed": fail, "skipped": skipped}
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
