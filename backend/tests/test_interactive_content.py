"""Content that only appears after a click, and pages that are never readable.

Two failures this pins:

1. **Pagination was structurally unreachable.** `?page=2` sat in `_NAV_QUERY_KEYS`
   for `-4.0`, which put it below the `score >= 0` cut in `_rank_children`. A
   listing was fetched once and the rest of its records discarded — while the
   crawler reported every page it *did* fetch as a success.

2. **A JS shell was mined for records.** When every rung failed to render, the
   thin static page was returned as `best` anyway, and the extractor read `NA`
   out of an empty shell and stored them as real records. `/api/intel/ask`
   refused this case; the run pipeline had no equivalent.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.providers.crawl import fetcher
from app.services import crawler as crawler_svc


class TestPaginationIsReachable:
    BASE = "https://x.example"

    def test_rel_next_is_harvested_even_though_it_is_not_an_anchor(self):
        """`<link rel="next">` is the site's own statement that more results
        exist. `find_all("a", href=True)` never saw it, because it is not an `<a>`."""
        html = ('<html><head><link rel="next" href="/companies?page=2"></head>'
                '<body><a href="/companies/acme">Acme</a></body></html>')
        pairs = fetcher.extract_links_with_text(html, self.BASE)
        assert any("page=2" in url for url, _ in pairs), pairs

    def test_a_next_page_outranks_the_records_it_lists(self):
        """On a listing page, the author's own links otherwise tie with `/page/2/`
        and — under the alphabetical tiebreak — win every per-parent slot, so the
        crawl read page one of a listing and never saw the rest of it."""
        page_link = crawler_svc.score_link(
            f"{self.BASE}/page/2", "Next", {}, set())
        author_link = crawler_svc.score_link(
            f"{self.BASE}/author/Jane-Austen", "Jane Austen", {}, set())
        assert page_link > author_link

    def test_path_pagination_survives_the_per_parent_trim(self):
        """The reservation in `_rank_children`. Without it the next page is
        ranked correctly and then cut anyway, which is the same bug wearing a
        different hat."""
        author = [(f"{self.BASE}/author/A{i}", f"Author {i}") for i in range(12)]
        ranked = crawler_svc._rank_children(
            author + [(f"{self.BASE}/page/2", "Next")],
            {"seed_domains": [], "allowed_sources": []},
            set(), set(), 0, 20, 8)
        assert f"{self.BASE}/page/2" in ranked
        assert len(ranked) <= 8

    def test_the_reservation_does_not_cost_a_slot_when_it_is_not_needed(self):
        """A page with no pagination keeps its full per-parent allowance."""
        plain = [(f"{self.BASE}/companies/c{i}", f"Company {i}") for i in range(6)]
        ranked = crawler_svc._rank_children(
            plain, {"seed_domains": [], "allowed_sources": []},
            set(), set(), 0, 20, 8)
        assert len(ranked) == 6

    def test_next_page_links_are_kept_by_the_ranker(self):
        """End-to-end through the cut that used to discard them."""
        ranked = crawler_svc._rank_children(
            [(f"{self.BASE}/companies?page=2", "Next"),
             (f"{self.BASE}/login", "Sign in")],
            {"seed_domains": [], "allowed_sources": []},
            set(), set(), 0, 10, 8)
        assert f"{self.BASE}/companies?page=2" in ranked
        assert f"{self.BASE}/login" not in ranked, (
            "sorting furniture is still dropped; only pagination was reinstated")

    def test_sort_and_filter_are_still_furniture(self):
        """The correction was narrow on purpose: `?sort=` re-renders records that
        are already held, so penalising it is still right."""
        for path in ("/companies?sort=name", "/companies?filter=active",
                     "/companies?order=desc"):
            assert crawler_svc.score_link(self.BASE + path, "Sort", {}, set()) < 0


class TestJsOnlyNavigation:
    BASE = "https://x.example"

    @pytest.mark.parametrize("html,expected", [
        # A card that navigates by attribute rather than href — the shape a
        # rendered listing grid actually uses.
        ('<div class="card" data-href="/companies/acme">Acme</div>',
         "/companies/acme"),
        ('<a onclick="location.href=\'/companies/globex\'">Globex</a>',
         "/companies/globex"),
        ('<span data-url="/companies/initech">Initech</span>',
         "/companies/initech"),
    ])
    def test_links_without_an_href_are_still_found(self, html, expected):
        pairs = fetcher.extract_links_with_text(html, self.BASE)
        assert any(expected in url for url, _ in pairs), pairs

    def test_non_http_schemes_are_ignored(self):
        """`href="#"` on an anchor is a reveal control, not a document."""
        html = ('<a href="javascript:void(0)" data-href="/companies/acme">A</a>'
                '<a href="mailto:x@y.example">Mail</a>'
                '<a href="#section">Section</a>')
        pairs = fetcher.extract_links_with_text(html, self.BASE)
        assert all(u.startswith(("http://", "https://")) for u, _ in pairs)


class TestRevealClicks:
    def test_the_expand_script_is_bounded_and_skips_consent_controls(self):
        """Each click is a round trip against a third party, so it is capped. And
        a cookie banner sits in the same visual layer as a reveal control —
        clicking one is exactly the wrong thing to do."""
        js = fetcher._EXPAND_JS
        assert str(fetcher._MAX_EXPAND_CLICKS) in js
        assert "cookie" in js.lower() and "consent" in js.lower()

    def test_navigating_links_are_never_clicked(self):
        """A click that navigates throws away the page being read."""
        js = fetcher._EXPAND_JS
        assert "navigates" in js or "getAttribute('href')" in js

    def test_the_click_budget_is_small(self):
        assert 1 <= fetcher._MAX_EXPAND_CLICKS <= 8


class TestUnrenderablePagesAreNamed:
    """`_one` returned the thin static shell as `best` when rendering failed,
    so the extractor produced confident records from an empty page."""

    SHELL = ("<html><head><script>window.app={}</script></head>"
             "<body><div id='root'></div></body></html>")

    def test_a_shell_surviving_every_rung_raises_rather_than_extracting(self):
        import asyncio

        async def _thin(url, method):
            return {"url": url, "html": self.SHELL, "markdown": "", "links": None}

        with pytest.raises(AppError) as exc:
            asyncio.run(crawler_svc._one(
                "https://x.example/", "http", _thin, asyncio.Semaphore(1)))
        assert exc.value.code == "E_RENDER_FAILED"
        # The message must say what happened and that nothing was read, so the
        # gap it creates downstream is explicable.
        assert "shell" in str(exc.value).lower()

    def test_a_real_page_is_returned_unchanged(self):
        import asyncio

        good = "<html><body><h1>Acme</h1><p>" + ("real content " * 200) + \
               "</p></body></html>"

        async def _ok(url, method):
            return {"url": url, "html": good, "markdown": good, "links": None}

        page = asyncio.run(crawler_svc._one(
            "https://x.example/", "http", _ok, asyncio.Semaphore(1)))
        assert page["html"] == good

    def test_the_threshold_is_the_same_one_the_escalation_uses(self):
        """Otherwise a page could be thin enough to be retried and long enough to
        be accepted, and the two rules would quietly disagree."""
        assert settings.FETCH_THIN_CHARS > 0