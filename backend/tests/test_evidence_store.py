"""The evidence substrate and the two traversal defects. No network.

What these lock in, all of it measured:

  * A page used to be fetched, reduced in RAM, extracted from and discarded.
    Only a SHA-256 and a character count survived, so every evidence quote and
    offset pointed at text that no longer existed. Pages are now stored, and
    the id comes back for records to cite.
  * Traversal gated on `page["html"]`, which the rendered rungs never return.
    A JS-heavy seed therefore never expanded a single subpage - precisely the
    pages that need following most - while their parsed links went unused.
  * `depth + 1 < max_depth` made RUN_MAX_DEPTH=2 mean one hop.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402


def run(coro):
    import asyncio
    return asyncio.run(coro)


def _page(url, html="", **extra):
    p = {"url": url, "final_url": url, "title": "t", "html": html,
         "text": "text", "method": "http"}
    p.update(extra)
    return p


async def _noop(_):
    pass


# --- evidence is stored -----------------------------------------------------

def test_settled_page_is_persisted_with_its_body_and_id():
    stored: list = []

    async def _persist_page(page):
        stored.append(page)
        return "page-1"

    async def _fetch(url, method):
        return _page(url, "<p>Acme has 240 employees</p>")

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop,
        _noop, {}, _persist_page))

    assert counts["successful"] == 1
    assert len(stored) == 1, "the page body was not stored"
    got = stored[0]
    assert got["url"] == "https://x.example/a"
    assert "Acme has 240 employees" in got["markdown"]
    assert got["depth"] == 0
    # The id comes back on the page dict so extraction can cite it.
    assert pages[0].get("page_id") == "page-1"


def test_rendered_page_markdown_is_what_gets_stored():
    """A Crawl4AI page has no `html`; the markdown is the real evidence."""
    stored: list = []

    async def _persist_page(page):
        stored.append(page)
        return "p1"

    async def _fetch(url, method):
        return {"url": url, "final_url": url, "title": "JS app", "method": "crawl4ai",
                "markdown": "# Rendered\n\nReal content from the browser.",
                "rendered_html": "<html>...</html>", "links": []}

    run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}], _fetch,
                              _noop, _noop, {}, _persist_page))
    assert "Real content from the browser." in stored[0]["markdown"]
    assert stored[0]["raw_html"] == "<html>...</html>"


def test_evidence_storage_failure_is_not_counted_as_success():
    """A run whose evidence could not be saved is not a trustworthy run, so the
    page is marked failed rather than silently kept as an unverifiable success."""
    sources: list = []

    async def _persist_page(page):
        raise RuntimeError("db down")

    async def _persist_source(s):
        sources.append(s)

    async def _fetch(url, method):
        return _page(url, "<p>content</p>")

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop,
        _persist_source, {}, _persist_page))
    assert counts["successful"] == 0
    assert counts["failed"] == 1
    assert not pages
    assert any("evidence not stored" in s.get("error", "") for s in sources)


def test_no_persist_page_still_works():
    """The probe/tests path passes no store; the crawl must not break."""

    async def _fetch(url, method):
        return _page(url, "<p>content</p>")

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop, {}))
    assert counts["successful"] == 1 and len(pages) == 1


# --- traversal reaches the pages that need it -------------------------------

def test_rendered_page_still_traverses_its_links():
    """The regression: no `html` key, yet it must still enqueue children.
    Its parsed links are post-JS, which is the better source anyway."""
    fetched: list = []

    async def _fetch(url, method):
        fetched.append(url)
        if url == "https://x.example/a":
            return {"url": url, "final_url": url, "title": "JS", "method": "crawl4ai",
                    "markdown": "seed", "rendered_html": "<html/>",
                    "links": ["https://x.example/b"]}
        return _page(url)

    pages, _ = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop,
        {"max_pages": 5, "traversal": {"max_pages_per_domain": 3}}))
    assert "https://x.example/b" in fetched
    assert {p["depth"] for p in pages} == {0, 1}


def test_depth_setting_means_the_number_of_hops():
    """`depth + 1 < max_depth` made RUN_MAX_DEPTH=2 mean ONE hop, because a
    child at depth 1 evaluated `2 < 2` and stopped. The limit now counts hops
    below the seed: RUN_MAX_DEPTH=3 over a->b->c->d reaches depth 3."""
    async def _fetch(url, method):
        nxt = {"a": "b", "b": "c", "c": "d"}
        tail = url.rsplit("/", 1)[-1]
        links = ([f"https://x.example/{nxt[tail]}"] if tail in nxt else [])
        return _page(url, "".join(f'<a href="{u}">x</a>' for u in links))

    orig = config_mod.settings.RUN_MAX_DEPTH
    try:
        config_mod.settings.RUN_MAX_DEPTH = 3
        pages, _ = run(crawler_svc.fetch_all(
            [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop,
            {"max_pages": 10, "traversal": {"max_pages_per_domain": 10}}))
        assert {p["depth"] for p in pages} == {0, 1, 2, 3}, (
            f"3 hops expected, got {sorted({p['depth'] for p in pages})}")

        # The invariant that matters, and that the old `depth + 1 < max_depth`
        # silently broke: the setting bounds the depth actually fetched. It
        # used to admit only max_depth - 1, so a configured 2 crawled one level.
        for limit in (1, 2, 3, 4):
            config_mod.settings.RUN_MAX_DEPTH = limit
            got, _ = run(crawler_svc.fetch_all(
                [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop,
                {"max_pages": 20, "traversal": {"max_pages_per_domain": 20}}))
            deepest = max(p["depth"] for p in got)
            assert deepest <= limit, (
                f"RUN_MAX_DEPTH={limit} fetched depth {deepest}")
    finally:
        config_mod.settings.RUN_MAX_DEPTH = orig


def test_child_records_its_parent():
    """parent_url is what makes the stored crawl tree reconstructable."""
    seen: dict = {}

    async def _persist_page(page):
        seen[page["url"]] = page.get("parent_url", "")
        return "p"

    async def _fetch(url, method):
        tail = url.rsplit("/", 1)[-1]
        if tail == "a":
            return _page(url, '<a href="https://x.example/b">b</a>')
        return _page(url)

    run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}], _fetch,
                              _noop, _noop, {"max_pages": 5,
                                             "traversal": {"max_pages_per_domain": 3}},
                              _persist_page))
    assert seen.get("https://x.example/b") == "https://x.example/a"
    assert seen.get("https://x.example/a") == ""


def test_malformed_html_does_not_kill_the_crawl():
    async def _fetch(url, method):
        if url.endswith("/a"):
            return _page(url, "<a href=<<>broken")
        return _page(url)

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop,
        {"max_pages": 4, "traversal": {"max_pages_per_domain": 3}}))
    assert counts["successful"] >= 1
