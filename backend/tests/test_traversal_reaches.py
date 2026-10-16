"""A crawl must actually go somewhere.

The bug these pin: seeds and children drew on one budget, and seeds won.
`discovery` returns up to `max_pages` seeds, `_worker` counts every claim against
`max_pages`, and seeds are exempt from the per-domain cap — so a normal run
entered the queue with `seeds == budget` and drained without a single child ever
being popped. `RUN_MAX_DEPTH` was honoured, reachable and completely unused.

Nothing failed. Every page fetched. `counts` said "successful". The dataset was
simply one level deep, and every downstream explanation attributed the missing
records to "crawl-depth" without ever being able to prove it.

So the assertions here are about **traversal happening at all**, and about
truncation being *visible* when it does not.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import crawler as crawler_svc


def _fetch_for(bodies: dict[str, str]):
    """A fetch that serves `bodies` and records every URL it was asked for."""
    seen: list[str] = []

    async def _fetch(url: str, method: str) -> dict:
        seen.append(url)
        return {"url": url, "html": bodies.get(url, "<html><body>nothing</body></html>"),
                "markdown": bodies.get(url, ""), "links": None}

    return _fetch, seen


async def _emit(_frame: dict) -> None:
    return None


def _persist(_url: str) -> None:
    return None


def _plan(**over) -> dict:
    plan = {"seed_domains": [], "allowed_sources": [], "max_pages": 6,
            "traversal": {"max_pages_per_domain": 10}}
    plan.update(over)
    return plan


LISTING = ('<html><body><h1>Companies</h1>'
           '<a href="/companies/acme">Acme</a>'
           '<a href="/companies/globex">Globex</a>'
           '<a href="/companies/initech">Initech</a>'
           '</body></html>')
DETAIL = '<html><body><h1>Company</h1><p>{}</p></body></html>'


class TestTraversalHappens:
    def test_children_are_fetched_when_seeds_would_have_filled_the_budget(self):
        """The regression, stated as the scenario that produced it.

        Four seeds against a budget of six. Before the reserve, discovery-sized
        seed lists exhausted the budget at the seed layer and `depth` was never
        more than 0.
        """
        bodies = {"https://x.example/list": LISTING}
        for slug in ("acme", "globex", "initech"):
            bodies[f"https://x.example/companies/{slug}"] = DETAIL.format(slug)
        fetch, seen = _fetch_for(bodies)

        seeds = [{"url": "https://x.example/list", "title": ""}]
        pages, counts = asyncio.run(crawler_svc.fetch_all(
            seeds, fetch, _emit, _persist, _plan()))

        trav = counts["traversal"]
        assert trav["traversed_pages"] > 0, (
            "the crawl never left its seeds; this is the whole bug")
        assert trav["depth_reached"] >= 1
        assert any("/companies/" in u for u in seen), seen

    def test_seed_intake_is_capped_so_traversal_space_is_reserved(self):
        """`SEED_BUDGET_SHARE` of the budget may go to seeds. The rest is the
        only guarantee that `max_depth` can mean anything."""
        bodies = {f"https://x.example/s{i}": DETAIL.format(str(i)) for i in range(20)}
        fetch, _seen = _fetch_for(bodies)
        seeds = [{"url": f"https://x.example/s{i}", "title": ""} for i in range(20)]

        pages, counts = asyncio.run(crawler_svc.fetch_all(
            seeds, fetch, _emit, _persist, _plan(max_pages=10)))

        trav = counts["traversal"]
        cap = max(1, int(10 * crawler_svc.SEED_BUDGET_SHARE))
        assert trav["seeds_queued"] <= cap, trav
        assert trav["seeds_dropped"] == 20 - trav["seeds_queued"]
        # The whole point: a share of the budget is still unspent for children.
        assert trav["seeds_queued"] < 10

    def test_a_single_seed_leaves_room_to_traverse(self):
        """The common case: one seed, one link, and the depth limit is honoured."""
        bodies = {"https://x.example/": LISTING,
                  "https://x.example/companies/acme": DETAIL.format("acme"),
                  "https://x.example/companies/globex": DETAIL.format("globex"),
                  "https://x.example/companies/initech": DETAIL.format("initech")}
        fetch, seen = _fetch_for(bodies)

        pages, counts = asyncio.run(crawler_svc.fetch_all(
            [{"url": "https://x.example/", "title": ""}], fetch, _emit, _persist,
            _plan(max_pages=5)))

        assert counts["traversal"]["traversed_pages"] >= 3
        assert counts["traversal"]["depth_reached"] == 1


class TestTruncationIsVisible:
    """The second half of the fix. A crawl that stops early must be able to say
    why, because `coverage` blames "crawl-depth" and that blame was unfounded."""

    def test_the_report_names_the_caps_in_force(self):
        bodies = {"https://x.example/": DETAIL.format("x")}
        fetch, _seen = _fetch_for(bodies)
        _pages, counts = asyncio.run(crawler_svc.fetch_all(
            [{"url": "https://x.example/", "title": ""}], fetch, _emit, _persist,
            _plan(max_pages=4, traversal={"max_pages_per_domain": 7})))
        trav = counts["traversal"]
        for key in ("seeds_queued", "seeds_dropped", "traversed_pages",
                    "blocked_by_domain_cap", "blocked_by_budget",
                    "max_depth", "depth_reached", "per_domain_cap", "budget"):
            assert key in trav, f"a run cannot explain itself without {key}"

    def test_the_per_domain_cap_is_counted_rather_than_dropped_in_silence(self):
        """It was a bare `return`: a run stopped at the cap was indistinguishable
        from a run with nothing left to follow.

        Accounting is exact rather than approximate: the page contributes up to
        `per_parent_cap` (8) children, the cap admits 2 of them once the seed
        has taken its slot, and the remaining 6 must be reported.
        """
        offered = 12
        bodies = {"https://x.example/": (
            '<html><body>' + "".join(
                f'<a href="/companies/c{i}">Company {i}</a>'
                for i in range(offered)
            ) + '</body></html>')}
        for i in range(offered):
            bodies[f"https://x.example/companies/c{i}"] = DETAIL.format(str(i))
        fetch, _seen = _fetch_for(bodies)

        _pages, counts = asyncio.run(crawler_svc.fetch_all(
            [{"url": "https://x.example/", "title": ""}], fetch, _emit, _persist,
            _plan(max_pages=20, traversal={"max_pages_per_domain": 3})))
        trav = counts["traversal"]
        assert trav["per_domain_cap"] == 3

        per_parent = 8  # `max_children_per_page`, the default
        queued = min(offered, per_parent)
        admitted = trav["per_domain_cap"] - 1  # the seed already holds a slot
        assert trav["traversed_pages"] == admitted, trav
        assert trav["blocked_by_domain_cap"] == queued - admitted, trav
        # Every candidate is accounted for: fetched, or blocked and reported.
        assert (trav["traversed_pages"] + trav["blocked_by_domain_cap"]) == queued

    def test_the_default_per_domain_cap_is_ten_not_three(self):
        """Three pages of one host was low enough to block recursion into the
        single most relevant site on a live run."""
        bodies = {"https://x.example/": DETAIL.format("x")}
        fetch, _seen = _fetch_for(bodies)
        _p, counts = asyncio.run(crawler_svc.fetch_all(
            [{"url": "https://x.example/", "title": ""}], fetch, _emit, _persist,
            _plan(max_pages=4)))
        assert counts["traversal"]["per_domain_cap"] == 10

    def test_a_flat_crawl_is_distinguishable_from_a_deep_one(self):
        """Two runs can both report "5 successful". Only one went anywhere."""
        deep = {"https://x.example/": LISTING}
        for slug in ("acme", "globex", "initech"):
            deep[f"https://x.example/companies/{slug}"] = DETAIL.format(slug)
        fetch_deep, _ = _fetch_for(deep)
        _p1, c_deep = asyncio.run(crawler_svc.fetch_all(
            [{"url": "https://x.example/", "title": ""}], fetch_deep, _emit,
            _persist, _plan(max_pages=6)))

        fetch_flat, _ = _fetch_for({f"https://x.example/s{i}": DETAIL.format(str(i))
                                    for i in range(6)})
        _p2, c_flat = asyncio.run(crawler_svc.fetch_all(
            [{"url": f"https://x.example/s{i}", "title": ""} for i in range(6)],
            fetch_flat, _emit, _persist, _plan(max_pages=6)))

        assert c_deep["traversal"]["traversed_pages"] > 0
        assert c_deep["traversal"]["depth_reached"] >= 1
        assert c_flat["traversal"]["traversed_pages"] == 0
        assert c_flat["traversal"]["depth_reached"] == 0
        # The traversal counters separate these two runs. (Their `successful`
        # counts do not — and cannot, because the seed reserve legitimately
        # trims the flat run's six seeds to two. That asymmetry is the fix
        # working, not a bug, so the assertion is on depth rather than totals.)
        assert (c_deep["traversal"]["depth_reached"]
                != c_flat["traversal"]["depth_reached"])
        assert c_flat["traversal"]["seeds_dropped"] > 0


class TestLinksSurviveTheHandoff:
    def test_dict_shaped_rendered_links_are_used(self):
        """`fetcher._crawl_result_links` emits `{"url","text","internal"}` and
        `_child_links_with_text` had no branch for it, so every link the renderer
        had already parsed — including post-JS ones no static parse can see — was
        discarded. Children survived only via a second parse capped at 50."""
        page = {"url": "https://x.example/", "markdown": "listing", "html": "",
                "links": [{"url": "https://x.example/companies/acme",
                           "text": "Acme", "internal": True},
                          {"url": "https://x.example/companies/globex",
                           "text": "Globex", "internal": True}]}
        pairs = crawler_svc._child_links_with_text(page)
        assert ("https://x.example/companies/acme", "Acme") in pairs
        assert ("https://x.example/companies/globex", "Globex") in pairs

    @pytest.mark.parametrize("shape", [
        [("https://x.example/a", "A")],
        ["https://x.example/a"],
        [{"url": "https://x.example/a", "text": "A"}],
    ])
    def test_every_link_shape_the_fetcher_can_emit_is_accepted(self, shape):
        page = {"url": "https://x.example/", "markdown": "", "html": "",
                "links": shape}
        assert crawler_svc._child_links_with_text(page)

    def test_duplicates_across_nav_bars_are_still_deduped(self):
        """Dedupe exists because a flat rendered list repeats one URL once per
        nav bar and each repeat spent a budget slot."""
        page = {"url": "https://x.example/", "markdown": "", "html": "",
                "links": [{"url": "https://x.example/a", "text": "A"},
                          {"url": "https://x.example/a", "text": "A again"}]}
        pairs = crawler_svc._child_links_with_text(page)
        assert len([u for u, _ in pairs]) == 1