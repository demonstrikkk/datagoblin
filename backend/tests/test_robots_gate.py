"""A candidate we may not crawl is dead on arrival, so screen it out early.

One live run accepted two sources, both `robots-disallowed`, spent the entire
runtime budget, and returned 0 records - with nothing to backfill from, because
nothing else had been screened in. The crawler was right to refuse them; the
waste was accepting them in the first place.

The gate runs BEFORE the Jev-A screen on purpose: the per-host robots cache makes
a repeat check effectively free, while a judge call is a real round trip to spend
on a URL that was never going to be fetched.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _deps(blocked=(), max_pages=3, screened_calls=None):
    """Deps whose robots gate refuses every URL in `blocked`."""
    allowed = set(blocked)

    async def _robots_ok(url):
        if screened_calls is not None:
            screened_calls.append(url)
        return url not in allowed

    async def _jev(url, title, snippet, entity):
        return {"judgment": "YES", "confidence": 0.9}

    async def _triage(url):
        return "web:http"

    return SimpleNamespace(robots_ok=_robots_ok, jev_screen=_jev,
                           triage_fn=_triage, max_pages=max_pages)


def _cands(*urls):
    return [{"url": u, "title": f"t{i}", "snippet": ""} for i, u in enumerate(urls)]


def test_robots_disallowed_candidate_is_never_judged():
    """The saving: no model call is spent on a URL that cannot be fetched."""
    calls = []
    deps = _deps(blocked={"https://blocked.test/a"}, screened_calls=calls)
    out = run(graph.screen_node(
        {"candidate_urls": _cands("https://blocked.test/a")}, deps))
    assert out["screened_urls"] == ["https://blocked.test/a"], (
        "it must be marked screened, or a later round reconsiders it")
    assert out["accepted_sources"] == []


def test_blocked_candidate_frees_the_slot_for_the_next_one():
    """The backfill: the cap must fill with crawlable alternatives."""
    deps = _deps(blocked={"https://blocked.test/a"}, max_pages=1)
    out = run(graph.screen_node({"candidate_urls": _cands(
        "https://blocked.test/a", "https://ok.test/b")}, deps))
    accepted = [a["url"] for a in out["accepted_sources"]]
    assert accepted == ["https://ok.test/b"], (
        "a blocked candidate consumed the only slot")


def test_robots_gate_runs_before_the_judge():
    order = []

    async def _robots_ok(url):
        order.append(("robots", url))
        return True

    async def _jev(url, title, snippet, entity):
        order.append(("jev", url))
        return {"judgment": "YES", "confidence": 0.9}

    deps = SimpleNamespace(robots_ok=_robots_ok, jev_screen=_jev,
                           triage_fn=lambda u: "web:http", max_pages=5)
    run(graph.screen_node({"candidate_urls": _cands("https://x.test/a")}, deps))
    assert order[0][0] == "robots", "the cheap check must come first"


def test_robots_is_checked_for_every_candidate_not_just_the_first():
    deps = _deps(blocked={"https://blocked.test/b"}, max_pages=5)
    out = run(graph.screen_node({"candidate_urls": _cands(
        "https://ok.test/a", "https://blocked.test/b")}, deps))
    assert [a["url"] for a in out["accepted_sources"]] == ["https://ok.test/a"]
    assert set(out["screened_urls"]) == {"https://ok.test/a", "https://blocked.test/b"}


def test_a_missing_robots_gate_does_not_block_the_run():
    """Absent capability must not mean "nothing is crawlable"."""
    async def _jev(url, title, snippet, entity):
        return {"judgment": "YES", "confidence": 0.9}
    deps = SimpleNamespace(jev_screen=_jev, triage_fn=lambda u: "web:http",
                           max_pages=5)  # no robots_ok at all
    out = run(graph.screen_node({"candidate_urls": _cands("https://x.test/a")}, deps))
    assert len(out["accepted_sources"]) == 1


def test_a_raising_robots_gate_defers_to_the_fetch():
    """Uncertainty must not silently cost yield; the fetcher re-checks anyway."""
    async def _boom(url):
        raise RuntimeError("robots unreachable")

    async def _jev(url, title, snippet, entity):
        return {"judgment": "YES", "confidence": 0.9}
    deps = SimpleNamespace(robots_ok=_boom, jev_screen=_jev,
                           triage_fn=lambda u: "web:http", max_pages=5)
    out = run(graph.screen_node({"candidate_urls": _cands("https://x.test/a")}, deps))
    assert len(out["accepted_sources"]) == 1, (
        "an uncertain robots check must allow, not block")


def test_already_screened_candidates_are_not_rechecked():
    seen = []

    async def _robots_ok(url):
        seen.append(url)
        return True

    async def _jev(url, title, snippet, entity):
        return {"judgment": "YES", "confidence": 0.9}
    deps = SimpleNamespace(robots_ok=_robots_ok, jev_screen=_jev,
                           triage_fn=lambda u: "web:http", max_pages=5)
    state = {"candidate_urls": _cands("https://x.test/a"),
             "screened_urls": ["https://x.test/a"]}
    out = run(graph.screen_node(state, deps))
    assert seen == [], "a screened URL must not be re-gated"
    assert out["accepted_sources"] == []


# --- the discovery dep wiring ----------------------------------------------

def test_discovery_wires_a_robots_gate(monkeypatch):
    """deps.robots_ok must exist, and it must go through the fetcher's cache."""
    import app.services.discovery as disc

    captured = {}

    async def _fake_robots(url):
        captured["url"] = url
        return True

    monkeypatch.setattr(disc.fetcher, "robots_allowed", _fake_robots)

    async def _fake_supervisor(state, deps, run_id):
        captured["deps"] = deps
        return {}

    monkeypatch.setattr(disc, "run_supervisor", _fake_supervisor)

    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _emit(ev):
        pass

    run(disc.discover("r1", {"goal": "g", "entity": "x", "search_queries": ["q"]},
                      _search, None, _emit))
    gate = captured.get("deps")
    assert gate is not None and callable(gate.robots_ok)
    assert run(gate.robots_ok("https://x.test/a")) is True
    assert captured["url"] == "https://x.test/a"
