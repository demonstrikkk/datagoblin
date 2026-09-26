"""Tavily discovery adapter. Blocking SDK isolated via to_thread + timeout (docs/35)."""
import asyncio
from typing import Any

from app.core.config import settings
from app.core.errors import provider_fatal, provider_transient


def _sync_search(api_key: str, query: str, limit: int, depth: str, topic: str,
                 include: list[str], exclude: list[str]) -> dict:
    from tavily import TavilyClient
    kwargs: dict = {}
    if include:
        kwargs["include_domains"] = include[:300]
    if exclude:
        kwargs["exclude_domains"] = exclude[:150]
    return TavilyClient(api_key=api_key).search(
        query, search_depth=depth, max_results=max(1, min(limit, 20)),
        topic=topic, include_answer=False, **kwargs)


async def search(query: str, limit: int = 5, include_domains: list[str] | None = None,
                 exclude_domains: list[str] | None = None) -> list[dict[str, Any]]:
    """Returns [{title, url, snippet}]. Raises AppError (never raw SDK errors)."""
    key = settings.TAVILY_API_KEY or ""
    if not key:
        raise provider_fatal("Discovery unavailable: TAVILY_API_KEY unset")
    q = (query or "")[:300]
    if not q.strip():
        raise provider_fatal("Discovery refused: empty query")
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(_sync_search, key, q, limit,
                              settings.TAVILY_SEARCH_DEPTH, settings.TAVILY_TOPIC,
                              include_domains or [], exclude_domains or []),
            timeout=30)
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise provider_transient(f"Tavily timeout: {e}")
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg or "unauthorized" in msg.lower():
            raise provider_fatal(f"Tavily auth failed: {msg[:150]}")
        if "429" in msg or "432" in msg or "433" in msg:
            raise provider_transient(f"Tavily rate-limited: {msg[:150]}")
        raise provider_transient(f"Tavily error: {msg[:150]}")
    out = []
    for r in (res.get("results", []) or [])[:limit]:
        url = str(r.get("url", ""))[:2000]
        if url.startswith(("http://", "https://")):
            out.append({"title": str(r.get("title", ""))[:300],
                        "url": url,
                        "snippet": str(r.get("content", ""))[:2000]})
    return out
