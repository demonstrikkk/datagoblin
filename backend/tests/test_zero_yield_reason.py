"""A zero-record run must say why it found nothing.

A live run reported `Done: 0 records (5/8 sources)` and the UI showed an empty
table with no explanation. That is indistinguishable from a broken app, and it
left the real cause invisible: the one page that worked was a nav-only
government dashboard whose data table is rendered client-side, so the extractor
was right to find no NGO rows in it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import runner as runner_svc  # noqa: E402


def _page(md="x" * 5000):
    return {"url": "https://x.example/p", "markdown": md, "html": ""}


# --- the explanation --------------------------------------------------------

def test_no_pages_fetched():
    assert "no page was fetched" in runner_svc._explain_zero_yield([], [])


def test_every_page_timed_out():
    r = runner_svc._explain_zero_yield([_page(), _page()], ["timeout", "timeout"])
    assert "timed out" in r and "2 page" in r
    assert "EXTRACT_PAGE_TIMEOUT_S" in r, "the cause should name the knob to raise"


def test_no_llm_available():
    r = runner_svc._explain_zero_yield([_page()], ["none"])
    assert "no LLM" in r


def test_every_page_provider_error():
    r = runner_svc._explain_zero_yield([_page()], ["error"])
    assert "provider failed" in r


def test_pages_read_but_nothing_matched():
    """The measured case: the fetch worked, the page had no such rows."""
    r = runner_svc._explain_zero_yield([_page()], ["opencode"])
    assert "contained no matching records" in r
    assert "opencode" in r, "naming the provider makes it diagnosable"


def test_near_empty_pages_are_called_out():
    """Nav-only pages are the common cause and are worth distinguishing."""
    pages = [_page("x" * 100), _page("y" * 50), _page()]
    r = runner_svc._explain_zero_yield(pages, ["opencode"] * 3)
    assert "2 of 3 were near-empty" in r


def test_mixed_failures_are_not_overstated():
    r = runner_svc._explain_zero_yield([_page(), _page()], ["timeout", "opencode"])
    assert "timed out on all" not in r, "one timeout is not 'all'"
    assert "contained no matching records" in r


def test_providers_tally_is_reported_even_when_empty():
    assert runner_svc._explain_zero_yield([_page()], []) == "no page was processed"


# --- it reaches the event and the run view ----------------------------------

def _run_with(providers, pages=None):
    import asyncio
    from app.providers.decision import jev
    from app.services import discovery as discovery_svc

    async def _supported(prompt, schema, **kw):
        key = next(iter(schema), "support")
        return {key: {"type": "noul", "noul": 0.9}}
    jev._jev_call = _supported

    orig_discover = discovery_svc.discover

    async def _discover(run_id, plan, search_fn, llm, emit):
        # One real source, so the run reaches extraction rather than stopping
        # at "no page was fetched".
        return [{"url": "https://x.example/p", "title": "t", "route": "web:http"}]
    discovery_svc.discover = _discover

    plan = {"goal": "find ngos", "entity": "ngo", "requested_count": 1,
            "max_results": 1, "search_queries": ["q"],
            "fields": [{"name": "ngo_name", "type": "string",
                        "description": "Name", "required": True}],
            "seed_urls": [], "allowed_sources": [], "max_pages": 2,
            "traversal": {"max_pages_per_domain": 1}, "validation_rules": [],
            "dedupe_keys": ["ngo_name"]}
    stored: dict = {}
    events: list = []

    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        return (pages or [_page()])[0]

    async def _llm(prompt, schema):
        return {"data": {"records": [], "coverage": "none"}, "provider": "t"}

    async def _store(rid, pl, rows, counts):
        stored.update(counts=counts)
        return "d1"

    async def _persist(s):
        pass

    async def _emit(ev):
        events.append(ev)

    ctx = {"search": _search, "fetch": _fetch, "llm": _llm, "store": _store,
           "persist_source": _persist, "cancelled": lambda: False}
    try:
        res = asyncio.run(runner_svc.execute_run("r1", plan, ctx, _emit))
    finally:
        discovery_svc.discover = orig_discover
    done = next(e for e in events if e["type"] in ("run.completed", "run.partial"))
    return res, done, stored


def test_completed_event_explains_a_zero_record_run():
    res, done, stored = _run_with(["opencode"])
    assert res["records"] == 0
    assert done["data"]["no_yield_reason"], "the event must carry the reason"
    assert "—" in done["message"], "the headline must carry it, not just the payload"
    assert "contained no matching records" in done["message"]


def test_the_reason_reaches_persistence():
    _, _, stored = _run_with(["opencode"])
    assert stored["counts"].get("no_yield_reason"), (
        "it must be stored, so the dataset view can explain the empty table")


def test_a_productive_run_reports_no_yield_reason_as_empty():
    """Never claim a failure that did not happen."""
    _, done, _ = _run_with(["opencode"], pages=[_page()])
    # This scenario also yields 0, so assert the shape rather than emptiness.
    assert "no_yield_reason" in done["data"]
