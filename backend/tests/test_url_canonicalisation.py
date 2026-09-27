"""A fragment is the browser's business, not the server's.

One live run stored five "pages" that were byte-identical copies of the same
7,915-character response: `page`, `page#/`, `page#/about-us`,
`page#/screen-reader`, `page#/login`. A fragment is resolved client-side and
never sent, so all five are one resource. 80% of the page budget went to one
document, and extraction then had four copies of a nav-only dashboard to work
with instead of four different pages.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _noop(_):
    pass


# --- the canonical form -----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://x.example/p", "https://x.example/p"),
    ("https://x.example/p#", "https://x.example/p"),
    ("https://x.example/p#/", "https://x.example/p"),
    ("https://x.example/p#/about-us", "https://x.example/p"),
    ("https://x.example/p#/login", "https://x.example/p"),
    ("https://x.example/p?a=1#frag", "https://x.example/p?a=1"),
    ("https://x.example/list?a=1&amp;b=2", "https://x.example/list?a=1&b=2"),
    ("https://x.example/list?a=1&#38;b=2", "https://x.example/list?a=1&b=2"),
])
def test_canonical_url_drops_the_fragment(raw, expected):
    assert crawler_svc.canonical_url(raw) == expected


def test_the_query_string_is_preserved():
    """The query genuinely selects content; only the fragment is noise."""
    assert crawler_svc.canonical_url(
        "https://x.example/s?per_page=100&state=7#top") == \
        "https://x.example/s?per_page=100&state=7"


def test_canonical_url_tolerates_junk():
    assert crawler_svc.canonical_url("") == ""
    assert crawler_svc.canonical_url("   ") == ""
    assert crawler_svc.canonical_url("not a url") == "not a url"
    assert crawler_svc.canonical_url("https://x.example/p#a#b") == "https://x.example/p"


# --- one document, one fetch ------------------------------------------------

def test_fragment_variants_are_fetched_once():
    """The exact waste: five stored rows that were one byte-identical page."""
    fetched: list = []

    async def _fetch(url, method):
        fetched.append(url)
        return {"url": url, "final_url": url, "title": "t",
                "html": '<a href="https://x.example/p#/login">l</a>'
                        '<a href="https://x.example/p#/about">a</a>',
                "method": "http"}

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": u, "title": ""} for u in [
            "https://x.example/p",
            "https://x.example/p#/",
            "https://x.example/p#/about-us",
            "https://x.example/p#/screen-reader",
            "https://x.example/p#/login",
        ]], _fetch, _noop, _noop, {"max_pages": 8}))

    assert counts["successful"] == 1, (
        f"expected one fetch of one document, got {counts['successful']}")
    assert len({p["url"] for p in pages}) == 1
    assert all("#" not in p["url"] for p in pages), "stored URL must be canonical"


def test_traversal_does_not_follow_client_side_routes():
    """`/#/login` is the page you are already on, not a child of it."""
    fetched: list = []

    async def _fetch(url, method):
        fetched.append(url)
        if url == "https://x.example/p":
            return {"url": url, "final_url": url, "title": "t", "method": "http",
                    "html": "".join(f'<a href="https://x.example/p#/{i}">x</a>'
                                    for i in range(5))}
        return {"url": url, "final_url": url, "title": "t", "html": "",
                "method": "http"}

    run(crawler_svc.fetch_all(
        [{"url": "https://x.example/p", "title": ""}], _fetch, _noop, _noop,
        {"max_pages": 8, "traversal": {"max_pages_per_domain": 5}}))
    assert fetched == ["https://x.example/p"], (
        f"client-side routes were followed: {fetched}")


def test_real_child_pages_are_still_followed():
    """Canonicalisation must not collapse genuinely different documents."""
    fetched: list = []

    async def _fetch(url, method):
        fetched.append(url)
        if url == "https://x.example/p":
            return {"url": url, "final_url": url, "title": "t", "method": "http",
                    "html": '<a href="https://x.example/child">c</a>'}
        return {"url": url, "final_url": url, "title": "t", "html": "",
                "method": "http"}

    run(crawler_svc.fetch_all(
        [{"url": "https://x.example/p", "title": ""}], _fetch, _noop, _noop,
        {"max_pages": 5, "traversal": {"max_pages_per_domain": 5}}))
    assert "https://x.example/child" in fetched, "a real child must still be crawled"


def test_seeded_fragment_variant_shares_the_seeds_slot():
    async def _fetch(url, method):
        return {"url": url, "final_url": url, "title": "t", "html": "",
                "method": "http"}

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/p", "title": ""},
         {"url": "https://x.example/p#top", "title": ""}],
        _fetch, _noop, _noop, {"max_pages": 4}))
    assert counts["successful"] == 1
    assert len(pages) == 1
