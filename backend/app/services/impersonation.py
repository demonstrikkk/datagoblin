"""Phase-2 impersonation rung: curl_cffi TLS impersonation, allowlist-gated.

Position in the waterfall: after plain httpx, before Crawl4AI (cheaper than a
browser) — and ONLY for hosts on IMPERSO_ALLOWLIST. Empty allowlist disables
the rung entirely: impersonate_fetch refuses off-list URLs even if wired wrong.

Hard policy guards (no exceptions, no params to override them):
- No proxies: this module accepts no proxy argument and passes none.
- No challenge-solving: 401/403/402/451 are fatal (no bypass, no retry-into-
  challenge, no Turnstile/CAPTCHA handling). Same status taxonomy as http.
- No deceptive headers: only what curl_cffi generates for the impersonation
  profile (no injected Google referer, no hand-forged sec-ch-ua).
- Full SSRF posture: aguard_url on the seed URL plus per-redirect-hop denial
  (redirects are followed manually, max 5, exactly so every hop is checked).
- Politeness preserved: robots check + per-host throttle, same as http_fetch.

Every impersonated fetch emits an audit record (host, profile, outcome,
status, error, timestamp) to structured logs; the page's method="impersonate"
also flows into persisted sources, so the audit trail is queryable per run.
"""
import datetime
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from app.core.config import settings
from app.core.errors import provider_fatal, provider_transient
from app.core.logging import log
from app.providers.crawl import fetcher
from app.services import politeness

_MAX_BYTES = 2_000_000
_MAX_TEXT = 100_000
_MAX_REDIRECTS = 5


def allowlist() -> list[str]:
    """Parsed IMPERSO_ALLOWLIST: lowercase host suffixes, no empties."""
    return [h.strip().lower().rstrip(".")
            for h in (settings.IMPERSO_ALLOWLIST or "").split(",")
            if h.strip()]


def is_allowlisted(url: str) -> bool:
    """Exact-or-suffix host match. Empty allowlist => always False."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return any(host == entry or host.endswith("." + entry)
               for entry in allowlist() if entry)


def audit_record(url: str, outcome: str, status_code: int = 0,
                 error: str = "") -> dict:
    """Pure audit entry for one impersonated fetch (logged + testable)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return {"event": "impersonation.fetch", "host": host,
            "profile": settings.IMPERSO_IMPERSONATE, "outcome": outcome,
            "status_code": status_code, "error": (error or "")[:200],
            "at": datetime.datetime.utcnow().isoformat() + "Z"}


def _status_from_message(msg: str) -> int:
    for probe in ("429", "401", "403", "402", "451", "500", "502", "503", "504"):
        if probe in str(msg):
            return int(probe)
    return 0


async def impersonate_fetch(url: str, timeout: int = 0) -> dict[str, Any]:
    """Fetch via curl_cffi impersonation. Page contract mirrors http_fetch."""
    from bs4 import BeautifulSoup
    from curl_cffi.requests import AsyncSession

    seed = url
    try:
        if not is_allowlisted(url):
            raise provider_fatal(
                f"Impersonation refused (host not allowlisted): {url[:120]}")
        target = await fetcher.aguard_url(url)
        host = (urlparse(target).hostname or "").lower()
        allowed, delay = await fetcher.robots_allowed(target)
        if not allowed:
            politeness.stats.note_blocked()
            log.info("impersonation fetch",
                     extra={"data": audit_record(target, "skipped",
                                                 error="robots-disallowed")})
            return {"url": target, "skipped": "robots-disallowed",
                    "method": "impersonate"}
        await fetcher.throttle(host, delay)

        profile = settings.IMPERSO_IMPERSONATE or "chrome"
        budget = timeout or settings.FETCH_HTTP_TIMEOUT_S
        try:
            session_ctx = AsyncSession(impersonate=profile)
        except Exception as e:
            if "impersonate" in str(e).lower():
                raise provider_fatal(
                    f"Impersonation profile rejected: {profile} ({str(e)[:100]})")
            raise provider_transient(f"Impersonation error: {str(e)[:150]}")
        t0 = time.monotonic()
        async with session_ctx as session:
            current = target
            status = 0
            response = None
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    response = await session.get(current, allow_redirects=False,
                                                 timeout=budget)
                except Exception as e:
                    if "impersonate" in str(e).lower():
                        raise provider_fatal(
                            f"Impersonation profile rejected: {profile} ({str(e)[:100]})")
                    politeness.observe(host, -1, time.monotonic() - t0)
                    raise provider_transient(f"Impersonation error: {str(e)[:150]}")
                status = response.status_code
                location = (response.headers.get("location", "") or "").strip()
                if status in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    # Per-hop SSRF denial (raises E_PROVIDER_FATAL on private).
                    await fetcher.aguard_url(current)
                    continue
                break
            if response is None:
                raise provider_transient("Impersonation error: no response")
            elapsed = time.monotonic() - t0
            nbytes = len(bytes(response.content or b""))
            if status == 429:
                politeness.observe(host, 429, elapsed, 0,
                                   politeness.parse_retry_after(
                                       response.headers.get("retry-after")))
                raise provider_transient(
                    f"HTTP 429 (rate-limited, backing off): {target[:100]}")
            if status in (401, 403, 402, 451):
                politeness.observe(host, status, elapsed)
                raise provider_fatal(f"HTTP {status} (no bypass): {target[:100]}")
            if status >= 400:
                politeness.observe(host, status, elapsed)
                raise provider_transient(f"HTTP {status}: {target[:100]}")
            ctype = (response.headers.get("content-type", "") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                kind = ctype.split(";")[0].strip() or "unknown"
                log.info("impersonation fetch",
                         extra={"data": audit_record(target, "skipped", status,
                                                     f"non-html:{kind}")})
                return {"url": target, "final_url": current[:2000],
                        "skipped": f"non-html:{kind}", "method": "impersonate"}
            html = bytes(response.content or b"")[:_MAX_BYTES].decode(
                "utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            for t in soup(["script", "style", "nav", "footer", "svg", "noscript",
                           "iframe"]):
                t.decompose()
            politeness.observe(host, 200, elapsed, nbytes)
            log.info("impersonation fetch",
                     extra={"data": audit_record(target, "ok", status)})
            return {"url": target, "final_url": current[:2000],
                    "title": (soup.title.string.strip() if soup.title and
                              soup.title.string else "")[:200],
                    "html": html, "text": soup.get_text(" ", strip=True)[:_MAX_TEXT],
                    "method": "impersonate"}
    except Exception as e:  # noqa: BLE001 (audit every failure, re-raise unchanged)
        from app.core.errors import AppError
        if isinstance(e, AppError):
            outcome = "refused" if e.code == "E_PROVIDER_FATAL" else "failed"
            msg = getattr(e, "message", str(e))
        else:
            outcome, msg = "failed", str(e)
        log.info("impersonation fetch",
                 extra={"data": audit_record(seed, outcome,
                                             _status_from_message(msg), str(msg)[:200])})
        raise
