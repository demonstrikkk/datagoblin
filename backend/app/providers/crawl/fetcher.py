"""Fetch waterfall: pooled httpx (SSRF-guarded, robots-aware, throttled) -> Crawl4AI.

Page contract: {url, final_url, title, markdown|html|text, method}
  or {url, skipped: <reason>} for robots-disallowed / non-HTML (normal, not failure).
Crawl4AI pages additionally carry raw_markdown, references (## References map
for <n> citations), and markdown_source (fit|raw|none) for the evidence chain.
Failure contract: AppError with method context; caller decides retry-alternate-skip.

Rate-limit posture (no proxies, no bypass — permitted sources only):
robots.txt per host (cached) -> per-host throttle (crawl-delay floor, 1s minimum)
-> exponential backoff+jitter on transient errors.
"""
import asyncio
import ipaddress
import random
import re
import socket
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings
from app.core.errors import AppError, provider_fatal, provider_transient
from app.services import politeness

_MAX_BYTES = 2_000_000
_MAX_TEXT = 100_000
_MIN_INTERVAL_S = 1.0
_ROBOTS_TTL_S = 3600

_client: httpx.AsyncClient | None = None
_robots: dict[str, tuple[urllib.robotparser.RobotFileParser, float, bool]] = {}
_last_hit: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}


def _client_pool() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            follow_redirects=True, max_redirects=5)
    return _client


def _host_lock(host: str) -> asyncio.Lock:
    lock = _host_locks.get(host)
    if lock is None:
        if len(_host_locks) > 2000:  # bound lock-table memory; oldest evicted
            _host_locks.pop(next(iter(_host_locks)))
        lock = _host_locks[host] = asyncio.Lock()
    return lock


async def _resolve_private(host: str) -> bool:
    """True when the host resolves to a private/loopback/link-local/reserved IP.

    Runs DNS off the event loop with a hard timeout; unresolvable => True
    (fail closed). Split out so the loop never blocks on DNS.
    """
    h = host.lower().strip().rstrip(".")
    if h in ("localhost",) or h.endswith((".localhost", ".internal", ".local")):
        return True
    try:
        parsed = ipaddress.ip_address(h)
        return (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_reserved)
    except ValueError:
        pass
    try:
        ip = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyname, h), timeout=5)
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except Exception:
        return True


def _host_is_private(host: str) -> bool:
    """Sync fast-path for literal IPs and obvious names (no DNS, never blocks)."""
    h = (host or "").lower().strip().rstrip(".")
    if h in ("localhost",) or h.endswith((".localhost", ".internal", ".local")):
        return True
    try:
        parsed = ipaddress.ip_address(h)
        return (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_reserved)
    except ValueError:
        return False  # hostname: resolved asynchronously by callers needing certainty


def guard_url(url: str) -> str:
    """SSRF guard, sync fast-path: scheme allowlist + literal-IP/private-name denial.
    Hostnames needing DNS use aguard_url (async, loop-safe). Raises E_PROVIDER_FATAL."""
    u = (url or "")[:2000].strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise provider_fatal(f"Fetch refused (scheme/host): {u[:120]}")
    if _host_is_private(p.hostname) or p.hostname == "169.254.169.254":
        raise provider_fatal(f"Fetch refused (private target): {p.hostname[:120]}")
    return u


async def aguard_url(url: str) -> str:
    """Full SSRF guard incl. DNS resolution off-loop. Raises E_PROVIDER_FATAL."""
    u = guard_url(url)
    host = urlparse(u).hostname or ""
    if await _resolve_private(host):
        raise provider_fatal(f"Fetch refused (private target): {host[:120]}")
    return u


async def _check_hops(response: httpx.Response) -> None:
    for hop in list(response.history or []) + [response]:
        host = urlparse(str(hop.url)).hostname or ""
        if _host_is_private(host) or await _resolve_private(host):
            raise provider_fatal(f"Fetch refused (redirect to private): {host[:120]}")


async def _fetch_robots_text(host: str, scheme: str) -> str | None:
    """Raw robots.txt body as seen by OUR user agent (pooled httpx client).

    Returns None when unreadable (caller allows: standard crawler behavior),
    "DISALLOW-ALL" on 401/403 (server refuses bots the rules themselves),
    else the body text. Evaluating rules fetched as our own UA (not
    Python-urllib's) is the correctness point: edge WAFs serve UA-specific
    robots files, and rules meant for another agent must not gate us.
    """
    try:
        r = await _client_pool().get(
            f"{scheme}://{host}/robots.txt",
            headers={"User-Agent": settings.FETCH_USER_AGENT}, timeout=10)
        if r.status_code in (401, 403):
            return "DISALLOW-ALL"
        if r.status_code >= 400:
            return None
        return r.text
    except Exception:
        return None


async def robots_allowed(url: str) -> tuple[bool, float]:
    """Returns (allowed, crawl_delay_s). Cached per host for 1h. Never raises."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        now = time.monotonic()
        cached = _robots.get(host)
        if cached and now - cached[1] < _ROBOTS_TTL_S:
            rp, ok = cached[0], cached[2]
        else:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{p.scheme or 'https'}://{host}/robots.txt")
            text = await _fetch_robots_text(host, p.scheme or "https")
            if text is None:
                ok = False
            else:
                if text == "DISALLOW-ALL":
                    rp.disallow_all = True
                else:
                    rp.parse(text.splitlines())
                ok = True
            _robots[host] = (rp, now, ok)
        if not ok:
            return True, 0.0  # unreadable robots => allow (standard), throttle still applies
        ua = settings.FETCH_USER_AGENT
        try:
            delay = float(rp.crawl_delay(ua) or 0)
        except Exception:
            delay = 0.0
        return bool(rp.can_fetch(ua, url)), max(0.0, min(delay, 30.0))
    except Exception:
        return True, 0.0


async def throttle(host: str, floor_s: float = 0.0) -> None:
    """Per-host minimum interval: 1s floor, robots delay, + adaptive AutoThrottle.

    The adaptive component only ever ADDS delay (slow/struggling hosts back
    off automatically; fast hosts stay at the floor). Never raises.
    """
    host = (host or "").lower()
    async with _host_lock(host):
        floor = max(_MIN_INTERVAL_S, floor_s, politeness.delay_for(host))
        wait = floor - (time.monotonic() - _last_hit.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[host] = time.monotonic()


async def backoff(attempt: int) -> None:
    """Exponential backoff + jitter: ~1s, ~2s, ~4s (capped 8s)."""
    await asyncio.sleep(min(8.0, 2.0 ** attempt) + random.uniform(0, 0.5))


def extract_links(html: str, base_url: str, limit: int = 50) -> list[str]:
    """Same-host links only (bounded in-domain traversal fuel). Pure function."""
    from bs4 import BeautifulSoup
    try:
        base_host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return []
    out: list[str] = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for a in soup.find_all("a", href=True):
            try:
                abs_url = urljoin(base_url, str(a["href"]).strip())
            except Exception:
                continue
            p = urlparse(abs_url)
            if p.scheme not in ("http", "https"):
                continue
            host = (p.hostname or "").lower()
            if host != base_host and not host.endswith("." + base_host):
                continue
            if abs_url not in out:
                out.append(abs_url)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def extract_all_links(html: str, base_url: str, limit: int = 200) -> list[str]:
    """All http(s) links incl. off-host (map/discovery fuel). Pure function."""
    from bs4 import BeautifulSoup
    out: list[str] = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for a in soup.find_all("a", href=True):
            try:
                abs_url = urljoin(base_url, str(a["href"]).strip())
            except Exception:
                continue
            p = urlparse(abs_url)
            if p.scheme not in ("http", "https") or not p.hostname:
                continue
            if abs_url not in out:
                out.append(abs_url[:2000])
            if len(out) >= max(1, limit):
                break
    except Exception:
        pass
    return out


def extract_media(html: str, base_url: str, limit: int = 100) -> dict:
    """Images + videos + embeds on the page. Pure function, no downloads.

    Images: img src (data-src fallback for lazy loaders) + alt text.
    Videos: video/source src, youtube/vimeo iframes, video links.
    Binaries are NEVER fetched — URLs + alts only (grounded references).
    """
    from bs4 import BeautifulSoup
    images: list[dict] = []
    videos: list[str] = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for img in soup.find_all("img"):
            try:
                src = (img.get("src") or img.get("data-src") or "").strip()
                if not src or src.startswith("data:"):
                    continue
                abs_src = urljoin(base_url, src)
                p = urlparse(abs_src)
                if p.scheme not in ("http", "https"):
                    continue
                entry = {"src": abs_src[:2000],
                         "alt": str(img.get("alt", "") or "").strip()[:300]}
                if entry not in images:
                    images.append(entry)
                if len(images) >= limit:
                    break
            except Exception:
                continue
        for tag in soup.find_all(["video", "source", "iframe"]):
            try:
                src = (tag.get("src") or "").strip()
                if not src:
                    continue
                abs_src = urljoin(base_url, src)
                p = urlparse(abs_src)
                if p.scheme not in ("http", "https"):
                    continue
                if abs_src not in videos:
                    videos.append(abs_src[:2000])
                if len(videos) >= limit:
                    break
            except Exception:
                continue
        for a in soup.find_all("a", href=True):
            try:
                href = str(a["href"]).strip()
                if not re.search(r"\.(mp4|webm|mov|m3u8|mp3|wav)(\?|#|$)", href,
                                 re.IGNORECASE):
                    continue
                abs_src = urljoin(base_url, href)
                if abs_src not in videos:
                    videos.append(abs_src[:2000])
            except Exception:
                continue
    except Exception:
        pass
    return {"images": images, "videos": videos[:limit]}


async def http_fetch(url: str, timeout: int = 0) -> dict[str, Any]:
    from bs4 import BeautifulSoup
    target = await aguard_url(url)
    host = (urlparse(target).hostname or "").lower()
    allowed, delay = await robots_allowed(target)
    if not allowed:
        politeness.stats.note_blocked()  # self-restraint counts as held-back
        return {"url": target, "skipped": "robots-disallowed", "method": "http"}
    await throttle(host, delay)
    t0 = time.monotonic()
    try:
        r = await _client_pool().get(
            target, headers={"User-Agent": settings.FETCH_USER_AGENT,
                             "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout or settings.FETCH_HTTP_TIMEOUT_S)
        await _check_hops(r)
        elapsed = time.monotonic() - t0
        if r.status_code == 429:
            politeness.observe(host, 429, elapsed, 0,
                               politeness.parse_retry_after(r.headers.get("retry-after")))
        elif r.status_code >= 400:
            politeness.observe(host, r.status_code, elapsed)
        r.raise_for_status()
    except AppError:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        politeness.observe(host, -1, time.monotonic() - t0)
        raise provider_transient(f"HTTP timeout {target[:100]}: {e}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise provider_transient(f"HTTP 429 (rate-limited, backing off): {target[:100]}")
        if e.response.status_code in (401, 403, 402, 451):
            raise provider_fatal(f"HTTP {e.response.status_code} (no bypass): {target[:100]}")
        raise provider_transient(f"HTTP {e.response.status_code}: {target[:100]}")
    except Exception as e:
        politeness.observe(host, -1, time.monotonic() - t0)
        raise provider_transient(f"HTTP error: {str(e)[:150]}")
    ctype = r.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        kind = ctype.split(";")[0].strip() or "unknown"
        return {"url": target, "final_url": str(r.url)[:2000], "skipped": f"non-html:{kind}",
                "method": "http"}
    body = await r.aread()
    politeness.observe(host, 200, elapsed, len(body))
    html = body[:_MAX_BYTES].decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "svg", "noscript", "iframe"]):
        t.decompose()
    media = extract_media(html, str(r.url)[:2000])
    return {"url": target, "final_url": str(r.url)[:2000],
            "title": (soup.title.string.strip() if soup.title and soup.title.string else "")[:200],
            "html": html, "text": soup.get_text(" ", strip=True)[:_MAX_TEXT],
            "images": media["images"], "videos": media["videos"],
            "method": "http"}


def _crawl_result_media(res: object, base_url: str) -> dict:
    """Normalize Crawl4AI result.media to extract_media shape. Never raises."""
    images: list[dict] = []
    videos: list[str] = []
    try:
        for m in (getattr(res, "media", None) or {}).get("images", []) or []:
            if not isinstance(m, dict):
                continue
            src = str(m.get("src", "") or "").strip()
            if not src:
                continue
            abs_src = urljoin(base_url, src)
            p = urlparse(abs_src)
            if p.scheme not in ("http", "https"):
                continue
            entry = {"src": abs_src[:2000],
                     "alt": str(m.get("alt", "") or m.get("desc", "") or "").strip()[:300]}
            if entry not in images:
                images.append(entry)
            if len(images) >= 100:
                break
        for m in (getattr(res, "media", None) or {}).get("videos", []) or []:
            src = str((m.get("src", "") if isinstance(m, dict) else m) or "").strip()
            if not src:
                continue
            abs_src = urljoin(base_url, src)
            if urlparse(abs_src).scheme in ("http", "https") and abs_src not in videos:
                videos.append(abs_src[:2000])
    except Exception:
        pass
    return {"images": images, "videos": videos[:100]}


def _crawl_result_links(res: object, base_url: str, limit: int = 200) -> list[dict]:
    """Crawl4AI result.links (internal/external + text) as link records."""
    out: list[dict] = []
    try:
        links = getattr(res, "links", None) or {}
        for group in ("internal", "external"):
            for l in links.get(group, []) or []:
                if not isinstance(l, dict):
                    continue
                href = str(l.get("href", "") or "").strip()
                if not href:
                    continue
                abs_url = urljoin(base_url, href)
                if urlparse(abs_url).scheme not in ("http", "https"):
                    continue
                out.append({"url": abs_url[:2000],
                            "text": str(l.get("text", "") or "").strip()[:200],
                            "internal": group == "internal"})
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


def _crawl_result_page(res: object, url: str) -> dict:
    """Map a Crawl4AI result to the page contract (pure, testable)."""
    md = getattr(res, "markdown", None)
    fit = (getattr(md, "fit_markdown", "") or "")[:_MAX_TEXT]
    raw = (getattr(md, "raw_markdown", "") or "")[:_MAX_TEXT]
    refs = getattr(md, "references_markdown", "") or ""
    # Phase-1 evidence chain: the LLM reads fit_markdown (pruned, cited);
    # raw + references are preserved for audit and quote/reference gates.
    source = "fit" if fit.strip() else ("raw" if raw.strip() else "none")
    # Full-snapshot capture: the complete rendered DOM (scripts, hidden
    # tab panels, hydrated components) is preserved as rendered_html.
    # Backend logic (reducer ladder, structured/script-JSON recovery,
    # evidence gates) decides what becomes context — nothing rendered
    # is discarded at capture time. Screenshots are NOT taken: no
    # vision consumer exists in the pipeline (text-only models).
    rendered = (getattr(res, "html", "") or "")[:_MAX_BYTES]
    # Deep-crawl media: Crawl4AI's parsed links/media, normalized to the
    # extract_media shape (URLs + alts, never binaries).
    media = _crawl_result_media(res, url)
    links = _crawl_result_links(res, url)
    return {"url": url, "final_url": url,
            "title": (getattr(res, "title", "") or "")[:200],
            "markdown": fit or raw, "raw_markdown": raw,
            "references": refs, "markdown_source": source,
            "rendered_html": rendered, "snapshot_chars": len(rendered),
            "images": media["images"], "videos": media["videos"],
            "links": links, "method": "crawl4ai"}


def _sync_crawl(url: str, timeout_s: int) -> dict:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
    from crawl4ai.content_filter_strategy import PruningContentFilterLXML
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    import asyncio as _aio

    async def _run() -> dict:
        gen = DefaultMarkdownGenerator(
            content_filter=PruningContentFilterLXML(threshold=0.4, threshold_type="fixed"))
        # Phase-6 bench finding: infinite-scroll pages (quotes.toscrape.com/scroll)
        # render thin without a full-page scan. scan_full_page costs seconds on
        # an already-expensive fallback path and rescues them deterministically.
        cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, markdown_generator=gen,
                               scan_full_page=True, scroll_delay=0.5)
        async with AsyncWebCrawler() as crawler:
            res = await _aio.wait_for(crawler.arun(url, config=cfg), timeout=timeout_s)
            if not res.success:
                raise RuntimeError(res.error_message or "crawl failed")
            return _crawl_result_page(res, url)
    return _aio.run(_run())


async def crawl4ai_fetch(url: str) -> dict[str, Any]:
    target = await aguard_url(url)
    host = (urlparse(target).hostname or "").lower()
    allowed, delay = await robots_allowed(target)
    if not allowed:
        politeness.stats.note_blocked()
        return {"url": target, "skipped": "robots-disallowed", "method": "crawl4ai"}
    await throttle(host, delay)
    try:
        # Outer cap covers browser launch too (inner timeout covers arun only).
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_crawl, target, settings.FETCH_CRAWL_TIMEOUT_S),
            timeout=settings.FETCH_CRAWL_TIMEOUT_S + 30)
    except (asyncio.TimeoutError, TimeoutError):
        raise provider_transient(f"Crawl4AI timeout ({settings.FETCH_CRAWL_TIMEOUT_S + 30}s cap)")
    except AppError:
        raise
    except Exception as e:
        raise provider_transient(f"Crawl4AI error: {str(e)[:150]}")


async def jina_fetch(url: str) -> dict[str, Any]:
    """Later adapter (FEATURE_JINA). Complete implementation, gated — not a stub."""
    if not settings.FEATURE_JINA:
        raise provider_fatal("Jina fallback disabled (FEATURE_JINA=false)")
    target = await aguard_url(url)
    headers = {"X-No-Cache": "true", "X-Timeout": "30"}
    if settings.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"
    try:
        r = await _client_pool().get(f"https://r.jina.ai/{target}", headers=headers, timeout=40)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise provider_transient(f"Jina {e.response.status_code}: {target[:100]}")
    except Exception as e:
        raise provider_transient(f"Jina error: {str(e)[:150]}")
    return {"url": target, "final_url": target, "title": "",
            "markdown": r.text[:_MAX_TEXT], "method": "jina"}


async def aclose_pool() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
