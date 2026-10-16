"""Source triage router — deterministic. No LLM control; optional classifier feeds hint only."""
from urllib.parse import urlparse

from app.core.errors import provider_fatal


def triage_source(url: str, content_type: str = "", llm_hint: str = "") -> str:
    """Route by content kind. Flags decide which rungs may run.

    `document:docling` leads only when FEATURE_DOCLING is on (and the optional
    package is installed); `web:jina` trails web routes only when FEATURE_JINA
    is on. Both default off, and the rungs themselves refuse with a named error
    when disabled — a route is never sent to a rung that cannot run.

    Pure scheme allowlist here (no DNS); full SSRF private-host denial happens in
    providers.crawl.fetcher.guard_url at fetch time.
    """
    u = (url or "")[:2000]
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise provider_fatal(f"Triage refused (scheme/host): {u[:120]}")
    lowered = u.lower()
    ct = (content_type or "").lower()
    if lowered.endswith(".pdf") or "application/pdf" in ct:
        return "document:docling"
    if any(lowered.endswith(e) for e in (".docx", ".pptx", ".xlsx")):
        return "document:docling"
    if "/api" in lowered or lowered.endswith(".json"):
        return "structured:api"
    # hint from future LLM classifier allowed, deterministic fallback otherwise
    if llm_hint in ("js-heavy", "needs-browser"):
        return "web:crawl4ai"
    return "web:http"  # MVP default; runner tries http→crawl4ai only (Later: +docling/jina/browser)

# MVP fetch order (Later adapters listed but not installed): http → crawl4ai.
FETCH_ORDER_MVP = ["web:http", "web:crawl4ai"]
FETCH_ORDER = ["web:http", "readable:trafilatura", "document:docling", "web:crawl4ai", "web:jina", "browser:stagehand"]  # full (Later)
