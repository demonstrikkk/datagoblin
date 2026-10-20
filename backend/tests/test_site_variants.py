"""One document, one slot, however many languages it is served in.

Measured on run `34c6d6ca`: 11 of its 12 new pages were the same railmarket
profile of one company under `de.`, `es.`, `cs.`, `ro.`, `it.`, `fr.`, `hu.`,
`ru.`, `pl.`, `tr.` and `uk.`. Each locale host counted as its own domain and
its own page, so a third of the crawl budget was spent re-reading one company in
eleven languages while 89 other entities got nothing. The crawl reported
`successful: 27` and looked healthy.

These tests pin the collapse at all three places a variant can enter: the seed
list, one page's children, and two different pages linking different locales of
the same path.
"""
from __future__ import annotations

import asyncio

from app.services import crawler as crawler_svc


def _fetch_for(bodies: dict[str, str]):
    seen: list[str] = []

    async def _fetch(url: str, method: str) -> dict:
        seen.append(url)
        return {"url": url,
                "html": bodies.get(url, "<html><body>nothing</body></html>"),
                "markdown": bodies.get(url, ""), "links": None}

    return _fetch, seen


async def _emit(_frame: dict) -> None:
    return None


def _persist(_url: str) -> None:
    return None


def _plan(**over) -> dict:
    plan = {"seed_domains": [], "allowed_sources": [], "max_pages": 10,
            "traversal": {"max_pages_per_domain": 15}}
    plan.update(over)
    return plan


def _seed(url: str) -> dict:
    return {"url": url, "title": ""}


class TestSiteHost:
    def test_a_language_label_is_not_a_site(self):
        assert crawler_svc._domain("https://de.example.com/x") == "example.com"
        assert crawler_svc._domain("https://pt-br.example.com/x") == "example.com"
        assert crawler_svc._domain("https://uk.example.com/x") == "example.com"
        assert crawler_svc._domain("https://www.de.example.com/x") == "example.com"

    def test_a_real_subdomain_is_left_alone(self):
        assert crawler_svc._domain("https://blog.example.com/x") == "blog.example.com"
        assert crawler_svc._domain("https://careers.example.com/x") == "careers.example.com"

    def test_a_public_suffix_is_not_mistaken_for_a_language_label(self):
        # `de.co.uk` is a host under a public suffix; stripping `de.` would
        # leave `co.uk`, which is not a site.
        assert crawler_svc._domain("https://de.co.uk/x") == "de.co.uk"

    def test_the_same_path_in_two_languages_has_one_key(self):
        assert (crawler_svc._site_key("https://de.example.com/profile/acme")
                == crawler_svc._site_key("https://fr.example.com/profile/acme"))

    def test_different_paths_stay_distinct(self):
        assert (crawler_svc._site_key("https://de.example.com/about")
                != crawler_svc._site_key("https://de.example.com/contact"))


class TestSeedCollapse:
    def test_eleven_languages_of_one_profile_spend_one_slot(self):
        urls = ([_seed(f"https://{loc}.example.com/eu/profile/alfil") for loc in
                 ("de", "es", "cs", "ro", "it", "fr", "hu", "ru", "pl", "tr", "uk")]
                + [_seed("https://other.example/a"), _seed("https://third.example/b")])
        bodies = {u["url"]: "<html><body>x</body></html>" for u in urls}
        fetch, seen = _fetch_for(bodies)

        _pages, counts = asyncio.run(crawler_svc.fetch_all(
            urls, fetch, _emit, _persist, _plan(max_pages=10)))

        trav = counts["traversal"]
        assert sum(1 for u in seen if "alfil" in u) == 1, seen
        assert trav["seeds_collapsed"] == 10
        assert trav["seeds_queued"] == 3

    def test_the_canonical_host_is_preferred_over_a_locale(self):
        urls = [_seed("https://de.example.com/profile/acme"),
                _seed("https://example.com/profile/acme")]
        bodies = {u["url"]: "<html><body>x</body></html>" for u in urls}
        fetch, seen = _fetch_for(bodies)

        asyncio.run(crawler_svc.fetch_all(
            urls, fetch, _emit, _persist, _plan(max_pages=6)))

        assert seen == ["https://example.com/profile/acme"], seen


class TestChildCollapse:
    def test_one_page_linking_two_locales_fetches_one(self):
        # The static extractor only returns same-host links and subdomains of
        # the page's host, so the locale variants of one path are what a real
        # page can hand over.
        listing = ('<html><body>'
                   '<a href="https://de.example.com/profile/acme">Acme DE</a>'
                   '<a href="https://fr.example.com/profile/acme">Acme FR</a>'
                   '<a href="/about">About</a>'
                   '</body></html>')
        bodies = {"https://example.com/list": listing,
                  "https://de.example.com/profile/acme": "<html><body>acme</body></html>",
                  "https://fr.example.com/profile/acme": "<html><body>acme</body></html>",
                  "https://example.com/about": "<html><body>about</body></html>"}
        fetch, seen = _fetch_for(bodies)

        asyncio.run(crawler_svc.fetch_all(
            [_seed("https://example.com/list")], fetch, _emit, _persist,
            _plan(max_pages=6)))

        assert sum(1 for u in seen if "/profile/acme" in u) == 1, seen

    def test_two_pages_linking_two_locales_fetch_one(self):
        """The per-page collapse cannot see this pair; the claim must."""
        body_a = '<html><body><a href="https://de.example.com/p/1">one</a></body></html>'
        body_b = '<html><body><a href="https://fr.example.com/p/1">one</a></body></html>'
        bodies = {"https://example.com/a": body_a,
                  "https://example.com/b": body_b,
                  "https://de.example.com/p/1": "<html><body>x</body></html>",
                  "https://fr.example.com/p/1": "<html><body>x</body></html>"}
        fetch, seen = _fetch_for(bodies)

        _pages, counts = asyncio.run(crawler_svc.fetch_all(
            [_seed("https://example.com/a"), _seed("https://example.com/b")],
            fetch, _emit, _persist, _plan(max_pages=8)))

        assert sum(1 for u in seen if "/p/1" in u) == 1, seen
        assert counts["traversal"]["site_variants_collapsed"] == 1

    def test_distinct_pages_on_one_site_are_still_both_fetched(self):
        listing = ('<html><body>'
                   '<a href="https://team.example.com/profile/a">A</a>'
                   '<a href="https://team.example.com/profile/b">B</a>'
                   '</body></html>')
        bodies = {"https://example.com/list": listing,
                  "https://team.example.com/profile/a": "<html><body>a</body></html>",
                  "https://team.example.com/profile/b": "<html><body>b</body></html>"}
        fetch, seen = _fetch_for(bodies)

        asyncio.run(crawler_svc.fetch_all(
            [_seed("https://example.com/list")], fetch, _emit, _persist,
            _plan(max_pages=6)))

        assert "https://team.example.com/profile/a" in seen, seen
        assert "https://team.example.com/profile/b" in seen, seen
