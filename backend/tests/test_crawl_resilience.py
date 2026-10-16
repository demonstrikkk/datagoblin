"""A crawl must lose at most one page, never all of them.

`fetch_all` fans out over `_WORKERS` coroutines sharing one queue and one `pages`
list. Before this suite, a single raise anywhere inside a worker propagated out
of the `asyncio.gather` that joined them, so `fetch_all` returned nothing and
every page already fetched was discarded — over one malformed URL or one unhappy
hook.

The optional hooks (`persist_source`, `emit`) made that trivially reachable:
`persist_page` was guarded and its two neighbours were not, so passing `None` for
one of them, or a hook that raised once, destroyed the run.

Counts are asserted as well as pages, because the failure mode this replaces was
*silent*: a crawl that returned fewer pages while still reporting
`successful == attempted` looked like a site with nothing to crawl.

Driven with `asyncio.run` rather than pytest-asyncio, matching the rest of the
suite (see test_api_key_gate.py) — no plugin dependency for one helper.
"""
from __future__ import annotations

import asyncio

from app.services import crawler

LISTING = """<html><body>
  <a href="/a">Alpha Corp revenue</a>
  <a href="/b">Beta LLC revenue</a>
  <a href="/c">Gamma Ltd revenue</a>
</body></html>"""

PLAN = {
    "fields": [{"name": "revenue", "type": "number"}],
    "search_queries": ["revenue"],
    "traversal": {"max_depth": 2},
}


def run(coro):
    """Drive one coroutine to completion, matching the suite's convention."""
    return asyncio.run(coro)


def child(url: str) -> dict:
    return {"url": url, "final_url": url, "title": "Child",
            "html": "<html><body>child body</body></html>",
            "markdown": "child text", "content_hash": "c" + url[-1],
            "method": "http"}


def fetch_recorder(calls: list[str]):
    async def fetch(url: str, method: str) -> dict:
        calls.append(url)
        return child(url)
    return fetch


def crawl(seeds, fetch=None, emit=None, persist_source=None, reuse=None,
          plan_extra=None):
    plan = dict(PLAN)
    plan.update(plan_extra or {})
    return crawler.fetch_all(seeds, fetch, emit, persist_source, plan,
                             None, reuse)


SEED = [{"url": "https://site.test/"}]


def stored_listing(url: str) -> dict:
    """What `find_page_by_url(url, include_html=True)` actually returns."""
    return {"id": "p1", "url": url, "title": "Listing", "markdown": "seed text",
            "raw_html": LISTING, "content_hash": "abc", "run_id": "r0"}


# ------------------------------------------------------------------ hook guards


def test_a_none_persist_source_does_not_destroy_the_crawl():
    calls: list[str] = []
    pages, counts = run(crawl(SEED, fetch=fetch_recorder(calls),
                              persist_source=None))
    assert pages, "a None hook must not cost the run its pages"
    assert counts["attempted"] >= 1
    assert calls, "the seed was never fetched"


def test_a_persist_source_that_raises_is_contained():
    calls: list[str] = []
    rows: list[dict] = []

    async def bad_persist(row: dict) -> None:
        rows.append(row)
        if len(rows) == 1:
            raise RuntimeError("hook exploded once")

    pages, counts = run(crawl(SEED, fetch=fetch_recorder(calls),
                              persist_source=bad_persist))
    assert pages, "one failing bookkeeping row must not discard the crawl"
    assert counts["successful"] == counts["attempted"]
    assert counts["failed"] == 0, (
        "a hook failure is not a fetch failure; counting it as one understates "
        "the yield and misleads every downstream report")
    assert counts.get("hook_failures", 0) >= 1, (
        "the hook failure must be reported, not hidden")


def test_a_persist_source_that_always_raises_still_returns_pages():
    calls: list[str] = []

    async def always_bad(row: dict) -> None:
        raise RuntimeError("every row")

    pages, counts = run(crawl(SEED, fetch=fetch_recorder(calls),
                              persist_source=always_bad))
    assert len(pages) >= 1
    assert counts["successful"] >= 1
    assert counts["failed"] == 0


def test_an_emit_that_raises_is_contained():
    """A broken SSE consumer must not be able to destroy a crawl."""
    async def bad_emit(payload: dict) -> None:
        raise RuntimeError("stream closed")

    calls: list[str] = []
    pages, _ = run(crawl(SEED, fetch=fetch_recorder(calls), emit=bad_emit))
    assert pages
    assert calls


def test_a_none_emit_is_legal():
    calls: list[str] = []
    pages, _ = run(crawl(SEED, fetch=fetch_recorder(calls), emit=None))
    assert pages


def test_successful_emit_receives_structured_source_fetched():
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    calls: list[str] = []
    run(crawl(SEED, fetch=fetch_recorder(calls), emit=emit))
    fetched = [e for e in events if e.get("type") == "source.fetched"]
    assert fetched, "no source.fetched event reached the consumer"
    assert fetched[0]["data"]["url"] == "https://site.test/"
    assert fetched[0]["data"]["ok"] is True


# ------------------------------------------------------------ worker resilience


def test_one_bad_url_does_not_lose_the_others():
    """The real shape of the bug: one poison URL, N-1 good ones."""
    async def flaky(url: str, method: str) -> dict:
        if "/boom" in url:
            raise ValueError("poison")
        return child(url)

    pages, counts = run(crawl(
        [{"url": "https://site.test/"}, {"url": "https://site.test/boom"}],
        fetch=flaky))
    assert counts["failed"] >= 1
    assert counts["successful"] >= 1, (
        "the good URL was lost because a different URL raised")
    assert pages


def test_counts_carry_the_documented_keys():
    calls: list[str] = []
    _, counts = run(crawl(SEED, fetch=fetch_recorder(calls)))
    assert set(counts) >= {"attempted", "successful", "failed", "skipped",
                           "reused", "fetched"}


# ------------------------------------------------------------------ reuse path


def test_a_reused_page_can_still_expand():
    """Reuse must not silently stop discovery."""
    fetched: list[str] = []

    async def reuse(url: str) -> dict:
        return stored_listing(url)

    pages, counts = run(crawl(SEED, fetch=fetch_recorder(fetched), reuse=reuse,
                              plan_extra={"reuse_stored_pages": True}))
    urls = {p["url"] for p in pages}
    assert "https://site.test/a" in urls, (
        "a reused listing page expanded to nothing, so every run after the "
        "first would shrink the dataset one stored page at a time")
    assert counts["reused"] >= 1


def test_reuse_never_fetches_the_reused_url():
    fetched: list[str] = []

    async def reuse(url: str) -> dict:
        return stored_listing(url)

    run(crawl(SEED, fetch=fetch_recorder(fetched), reuse=reuse,
              plan_extra={"reuse_stored_pages": True}))
    assert "https://site.test/" not in fetched


def test_a_failing_reuse_lookup_falls_through_to_a_real_fetch():
    """A broken lookup must cost a fetch, never a record."""
    calls: list[str] = []

    async def reuse(url: str) -> dict:
        raise RuntimeError("lookup down")

    pages, counts = run(crawl(SEED, fetch=fetch_recorder(calls), reuse=reuse,
                              plan_extra={"reuse_stored_pages": True}))
    assert calls, "a failed reuse lookup silently dropped the URL"
    assert counts["reused"] == 0
    assert pages


# ------------------------------------------------------------------ bookkeeping


def test_counts_add_up():
    calls: list[str] = []
    _, counts = run(crawl(SEED, fetch=fetch_recorder(calls)))
    assert counts["attempted"] == (counts["successful"] + counts["failed"]
                                   + counts["skipped"])
    assert counts["fetched"] == max(0, counts["successful"] - counts["reused"])


def test_a_robots_skipped_page_is_not_a_failure():
    async def blocked(url: str, method: str) -> dict:
        return {"url": url, "skipped": "robots-disallowed", "method": method}

    _, counts = run(crawl(SEED, fetch=blocked))
    assert counts["skipped"] == 1
    assert counts["failed"] == 0


def test_single_seed():
    calls: list[str] = []
    pages, counts = run(crawl(SEED, fetch=fetch_recorder(calls)))
    assert counts["attempted"] == 1
    assert len(pages) == 1


def test_many_seeds():
    calls: list[str] = []
    seeds = [{"url": f"https://s{i}.test/"} for i in range(4)]
    pages, counts = run(crawl(seeds, fetch=fetch_recorder(calls)))
    assert counts["attempted"] == 4
    assert len(pages) == 4


def test_fetch_all_returns_exactly_two_values():
    """Shape guard: every caller unpacks (pages, counts)."""
    result = run(crawl(SEED, fetch=fetch_recorder([])))
    assert isinstance(result, tuple)
    assert len(result) == 2
    pages, counts = result
    assert isinstance(pages, list)
    assert isinstance(counts, dict)