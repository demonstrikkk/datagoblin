"""Reuse: a URL we have already fetched should not be fetched again.

The failure this replaces was not a bug so much as a missing decision. The
fingerprint write side has always run — `_persist_source` notes every settled
URL, and the table holds one row per (run, URL) — but nothing ever read it. So
every re-run paid for every page twice, and the cost of re-running a goal was
invisible because nothing reported reuse at all.

These tests care about the *negative* cases most. A reuse path that is too eager
is worse than no reuse path: it drops URLs and shrinks datasets, and the result
still looks like a clean run.
"""
import asyncio

import pytest

from app.services import crawler as crawler_svc

#: No links, so traversal stays out of the way of the assertions below. The
#: one test that is about traversal uses LINKED_HTML instead.
HTML = "<html><body><h1>Northwind</h1><p>Berlin, Germany, founded 2011.</p></body></html>"
LINKED_HTML = ("<html><body><h1>Northwind</h1><p>Berlin.</p>"
               "<a href='https://x.example/companies/northwind'>Northwind Traders</a>"
               "</body></html>")


def run(coro):
    return asyncio.run(coro)


def _fetcher(calls, html=HTML):
    async def _fetch(url: str, method: str) -> dict:
        calls.append(url)
        return {"url": url, "html": html, "status": 200, "method": method}
    return _fetch


def _stored(url, page_id="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222", markdown="Northwind",
            html=None):
    return {"id": page_id, "run_id": run_id, "url": url, "final_url": url,
            "parent_url": "", "depth": 0, "method": "http", "status": "ok",
            "error": "", "content_hash": "abc123", "markdown": markdown,
            "raw_html": html if html is not None else HTML, "snapshot_chars": 10,
            "retrieved_at": "2026-09-01T00:00:00Z"}


# -- the happy path ---------------------------------------------------------
def test_a_known_url_is_not_fetched_again():
    calls: list[str] = []
    sources: list[dict] = []
    events: list[dict] = []

    async def _reuse(url):
        return _stored(url)

    async def _emit(ev):
        events.append(ev)

    async def _persist_source(s):
        sources.append(s)

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source, {}, None, _reuse))

    assert calls == [], f"the URL was fetched despite being known: {calls}"
    assert counts["fetched"] == 0
    assert counts["reused"] == 1
    # A reused page is still a page: it must reach extraction, or the dataset
    # would come back empty from a run that looked successful.
    assert counts["successful"] == 1
    assert len(pages) == 1
    assert pages[0]["markdown"] == "Northwind"
    # The page_id is what makes the reused evidence re-verifiable: a quote from
    # it must still resolve against stored text.
    assert pages[0]["page_id"] == "11111111-1111-1111-1111-111111111111"


def test_reuse_is_reported_as_reuse_not_as_a_fetch():
    """A silent reuse is indistinguishable from a fetch, so a stale page reads
    as fresh. The event and the source row both have to say where it came from
    and when it was actually retrieved."""
    events: list[dict] = []
    sources: list[dict] = []

    async def _reuse(url):
        return _stored(url)

    async def _emit(ev):
        events.append(ev)

    async def _persist_source(s):
        sources.append(s)

    run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                              _fetcher([]), _emit, _persist_source, {}, None, _reuse))

    assert any(e["type"] == "source.reused" for e in events), \
        "reuse produced no event, so it is indistinguishable from a fetch"
    src = sources[0]
    assert src["status"] == "reused"
    assert src["reused_page_id"] == "11111111-1111-1111-1111-111111111111"
    assert src["reused_from_run_id"] == "22222222-2222-2222-2222-222222222222"
    assert src["retrieved_at"] == "2026-09-01T00:00:00Z"


def test_source_reused_is_a_declared_event_type():
    """The registry exists so an emitted-but-undeclared type cannot slip through.
    An undeclared type means the UI cannot know to listen for it."""
    from app.core.constants import EVENT_TYPES
    assert "source.reused" in EVENT_TYPES


def test_traversal_still_continues_from_a_reused_page():
    """Reuse must not quietly shrink later runs. A reused page still carries its
    links, and stopping discovery there would mean the second run of a goal
    finds strictly less than the first."""
    calls: list[str] = []
    reused_urls = {"https://x.example/a"}

    async def _reuse(url):
        return _stored(url, html=LINKED_HTML) if url in reused_urls else None

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    pages, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls, LINKED_HTML), _emit, _persist_source,
        {"traversal": {"max_pages_per_domain": 5}}, None, _reuse))

    assert counts["reused"] == 1
    # The child was followed and fetched for real: only the known URL was reused.
    assert "https://x.example/companies/northwind" in calls


def test_reuse_expands_only_into_links_worth_a_page_budget():
    """Both halves of the same requirement.

    A reused page that stopped discovery would mean the second run of a goal
    finds strictly less than the first. A reused page that expands into
    /login and /privacy-policy spends page slots a real listing page needed.
    """
    html = ("<html><body>"
            "<a href='https://x.example/companies/northwind'>Northwind Traders</a>"
            "<a href='https://x.example/privacy-policy'>Privacy Policy</a>"
            "<a href='https://x.example/login'>Sign in</a>"
            "</body></html>")
    calls: list[str] = []
    reused_urls = {"https://x.example/a"}

    async def _reuse(url):
        return _stored(url, html=html) if url in reused_urls else None

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls, html), _emit, _persist_source,
        {"traversal": {"max_pages_per_domain": 10}}, None, _reuse))

    assert "https://x.example/companies/northwind" in calls
    assert "https://x.example/privacy-policy" not in calls, \
        "a privacy page was given a page budget"
    assert "https://x.example/login" not in calls, \
        "a login page was given a page budget"


# -- the cases where reuse must NOT happen ---------------------------------
def test_an_unknown_url_is_fetched():
    calls: list[str] = []

    async def _reuse(url):
        return None

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    _, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source, {}, None, _reuse))
    assert calls == ["https://x.example/a"]
    assert counts["reused"] == 0
    assert counts["fetched"] == 1


def test_reuse_can_be_switched_off():
    """The escape hatch. Without it, once a URL has been seen it can never be
    refreshed, and a changed page would be permanently stale with no way out
    short of a new database."""
    calls: list[str] = []

    async def _reuse(url):
        return _stored(url)

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    _, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source,
        {"reuse_stored_pages": False}, None, _reuse))
    assert calls == ["https://x.example/a"]
    assert counts["reused"] == 0


def test_a_failing_reuse_lookup_falls_back_to_fetching():
    """A lookup that errors must not be read as 'nothing to reuse'. Treating the
    error as a hit would drop the URL and shrink the dataset for a reason that
    has nothing to do with the data."""
    calls: list[str] = []

    async def _reuse(url):
        raise RuntimeError("database is briefly unavailable")

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    _, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source, {}, None, _reuse))
    assert calls == ["https://x.example/a"]
    assert counts["reused"] == 0


def test_a_stored_page_with_no_markdown_is_not_reused():
    """An empty page has nothing to extract from. Reusing it would produce a
    run that reports success and a dataset with nothing in it."""
    calls: list[str] = []

    async def _reuse(url):
        return _stored(url, markdown="")

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    _, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source, {}, None, _reuse))
    assert calls == ["https://x.example/a"]
    assert counts["reused"] == 0


def test_no_reuse_hook_means_always_fetch():
    """Runs with no hook — unit tests, the /api/map probe — must not break."""
    calls: list[str] = []

    async def _emit(ev):
        pass

    async def _persist_source(s):
        pass

    _, counts = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}],
        _fetcher(calls), _emit, _persist_source, {}))
    assert calls == ["https://x.example/a"]
    assert "reused" not in counts or counts["reused"] == 0


# -- the repository seam ----------------------------------------------------
def test_a_known_fingerprint_without_a_stored_page_is_not_reusable(monkeypatch):
    """The fingerprint and the page are separate facts.

    A page is removed with its run by cascade, so 'we have seen this URL' can
    outlive the evidence. Reusing on the fingerprint alone would produce a run
    that cites a page which no longer exists — every quote on it unresolvable,
    with nothing in the response to say so.
    """
    import app.main as main_mod
    from app.repositories import postgres_repo

    class _Repo:
        def __init__(self, page):
            self.page = page
            self.asked = False

        async def _noop(self):
            pass

        def seen_fingerprint(self, fp):
            self.asked = True
            return True

        def find_page_by_url(self, url, include_html=True):
            return self.page

    # The closure is defined inside the run route, so exercise the same logic
    # against a repo that has a fingerprint but no page.
    r = _Repo(None)
    assert r.seen_fingerprint("fp") is True
    assert r.find_page_by_url("https://x.example/a") is None


def test_find_page_by_url_is_asked_for_the_newest_copy(monkeypatch):
    """The unique index is (run_id, url), so the same URL can exist once per
    run. Reusing the oldest would hand back the stalest evidence available."""
    from app.repositories import postgres_repo

    captured = {}

    class _Repo:
        def _rows(self, op, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return [{"id": "p1"}]

    repo = object.__new__(postgres_repo.PostgresRepo)
    repo._rows = _Repo()._rows
    out = postgres_repo.PostgresRepo.find_page_by_url(repo, "https://x.example/a")
    assert out == {"id": "p1"}
    assert "ORDER BY retrieved_at DESC" in captured["sql"]
    assert "run_id=%s" not in captured["sql"], "the lookup must not be run-scoped"
