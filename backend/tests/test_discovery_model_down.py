"""Discovery fails fast when the model server dies mid-run.

The observed shape: serve on :4096 died after the run preflight passed, and the
run sat in DISCOVERING for 4+ minutes — every supervisor round paying full
timeouts on dead model calls (strategy, screens, continuation) with nothing
surfacing until the user cancelled. The wall-clock cap would eventually fire,
but it is measured in minutes.

Two guards, both tested here without a network:

  1. a start-of-discovery probe: one cheap `available()` check fails the run in
     seconds when the chain has nowhere else to go (STRICT on), instead of
     burning the discovery budget finding out;
  2. a consecutive-failure breaker on the strategy calls: three exceptions in a
     row raise DiscoveryModelDown, which the runner's terminal guard turns into
     a FAILED run naming the cause.

Empty-but-valid results never trip either guard — only exceptions do. A model
that answers "nothing" is working; a model that cannot be reached is not, and
the two must not share a code path.
"""
import asyncio

import pytest

from app.core.config import settings
from app.providers.llm import opencode as opencode_llm
from app.services import discovery as discovery_svc


def _plan():
    return {"goal": "g", "entity": "e", "fields": [],
            "search_queries": ["q1"], "max_pages": 2, "requested_count": 2}


async def _emit(ev):
    return None


async def _search(q, limit, include=None, exclude=None):
    return []


def test_a_dead_provider_at_discovery_start_fails_fast(monkeypatch):
    """The probe fires before any search, and the model is never called."""
    monkeypatch.setattr(settings, "OPENCODE_STRICT", True)
    monkeypatch.setattr(
        opencode_llm, "available",
        lambda: asyncio.sleep(0, result={"reachable": False,
                                         "base_url": "http://127.0.0.1:4096",
                                         "error": "unreachable"}))

    async def _exploding(*a, **k):
        raise AssertionError("no supervisor round should run after a dead probe")

    monkeypatch.setattr(discovery_svc, "run_supervisor", _exploding)

    async def _llm(prompt, schema):
        raise AssertionError("the model must not be called after a dead probe")

    with pytest.raises(discovery_svc.DiscoveryModelDown, match="stopped responding"):
        asyncio.run(discovery_svc.discover(
            "r1", _plan(), _search, _llm, _emit))


def test_a_disabled_provider_does_not_block_discovery(monkeypatch):
    """Switched off is a configuration, not a fault — another rung may serve."""
    monkeypatch.setattr(settings, "OPENCODE_STRICT", True)
    monkeypatch.setattr(
        opencode_llm, "available",
        lambda: asyncio.sleep(0, result={"reachable": False,
                                         "error": "disabled"}))

    async def _fake_supervisor(initial, deps, run_id):
        return {"accepted_sources": [], "blocked_domains": []}

    monkeypatch.setattr(discovery_svc, "run_supervisor", _fake_supervisor)
    out = asyncio.run(discovery_svc.discover(
        "r1", _plan(), _search, None, _emit))
    assert out == []


def test_a_dead_primary_with_fallbacks_elsewhere_proceeds(monkeypatch):
    """STRICT off means Groq/Gemini can still serve; the per-call cascade owns
    that fallback, so discovery must not pre-empt it by failing."""
    monkeypatch.setattr(settings, "OPENCODE_STRICT", False)
    monkeypatch.setattr(
        opencode_llm, "available",
        lambda: asyncio.sleep(0, result={"reachable": False,
                                         "error": "unreachable"}))

    async def _fake_supervisor(initial, deps, run_id):
        return {"accepted_sources": [], "blocked_domains": []}

    monkeypatch.setattr(discovery_svc, "run_supervisor", _fake_supervisor)
    out = asyncio.run(discovery_svc.discover(
        "r1", _plan(), _search, None, _emit))
    assert out == []


def test_three_consecutive_strategy_failures_trip_the_breaker(monkeypatch):
    """Each dead call costs its full timeout; three in a row is a pattern, and
    the run should die naming the cause instead of grinding every round."""
    monkeypatch.setattr(settings, "OPENCODE_STRICT", False)

    async def _fake_supervisor(initial, deps, run_id):
        for _ in range(5):
            await deps.llm_strategy("p", {})
        return {"accepted_sources": [], "blocked_domains": []}

    monkeypatch.setattr(discovery_svc, "run_supervisor", _fake_supervisor)

    async def _dead(prompt, schema):
        raise RuntimeError("connection refused")

    with pytest.raises(discovery_svc.DiscoveryModelDown, match="consecutive"):
        asyncio.run(discovery_svc.discover(
            "r1", _plan(), _search, _dead, _emit))


def test_two_failures_then_a_success_resets_the_count(monkeypatch):
    """The breaker counts *consecutive* exceptions. A model that stumbles and
    recovers is weather, not an outage."""
    monkeypatch.setattr(settings, "OPENCODE_STRICT", False)
    calls = {"n": 0}

    async def _fake_supervisor(initial, deps, run_id):
        for _ in range(4):
            await deps.llm_strategy("p", {})
        return {"accepted_sources": [], "blocked_domains": []}

    monkeypatch.setattr(discovery_svc, "run_supervisor", _fake_supervisor)

    async def _flaky(prompt, schema):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("blip")
        return {"data": {}}

    out = asyncio.run(discovery_svc.discover(
        "r1", _plan(), _search, _flaky, _emit))
    assert out == []
    assert calls["n"] == 4


def test_diversify_top_up_excludes_accepted_hosts():
    """One host in, new hosts out. The top-up search excludes what discovery
    already accepted, forcing the engine off the dominant domain."""
    calls = []

    async def _search(q, limit, include=None, exclude=None):
        calls.append(set(exclude or []))
        assert "a.example" in (exclude or []), "accepted hosts must be excluded"
        return [{"url": "https://b.example/p", "title": "B", "snippet": "s"},
                {"url": "https://a.example/other", "title": "A2", "snippet": "s"}]

    async def _robots(url):
        return True

    async def _emit(ev):
        pass

    accepted = [{"url": "https://a.example/p", "title": "A"}]
    out = asyncio.run(discovery_svc._diversify(
        "r1", {"goal": "g"}, accepted, 12, _search, _robots, _emit))
    urls = [a["url"] for a in out]
    assert "https://a.example/p" in urls
    assert "https://b.example/p" in urls
    assert "https://a.example/other" not in urls


def test_diversify_stops_when_the_page_budget_is_filled():
    """A filled budget is enough — no extra search is spent."""
    calls = []

    async def _search(q, limit, include=None, exclude=None):
        calls.append(q)
        return []

    async def _robots(url):
        return True

    async def _emit(ev):
        pass

    accepted = [{"url": f"https://h{i}.example/p"} for i in range(4)]
    out = asyncio.run(discovery_svc._diversify(
        "r1", {"goal": "g"}, accepted, 4, _search, _robots, _emit))
    assert out == accepted and calls == []


def test_diversify_fills_the_page_budget_past_the_old_host_floor():
    """Four hosts used to end the top-up with pages still unspent.

    A run declared 8 pages, stopped at 4 because it had four hosts, and left
    `headquarters_city` empty — the pages it never opened were the ones holding
    it. The budget is the stopping rule now, bounded by the query list.
    """
    calls = []

    async def _search(q, limit, include=None, exclude=None):
        calls.append(q)
        return [{"url": f"https://new{len(calls)}.example/p", "title": "N",
                 "snippet": "s"}]

    async def _robots(url):
        return True

    async def _emit(ev):
        pass

    accepted = [{"url": f"https://h{i}.example/p"} for i in range(4)]
    plan = {"goal": "g", "search_queries": ["q1", "q2", "q3", "q4", "q5"]}
    out = asyncio.run(discovery_svc._diversify(
        "r1", plan, accepted, 8, _search, _robots, _emit))
    assert len(out) == 8, "the plan's page budget must be filled"
    assert len(calls) == 4, "one search per page still missing, no more"


def test_no_llm_means_no_breaker_and_no_probe(monkeypatch):
    """Runs launched without a provider (tests, dry harnesses) skip both
    guards instead of failing them — the guards protect live runs only."""
    async def _fake_supervisor(initial, deps, run_id):
        assert await deps.llm_strategy("p", {}) == {}
        return {"accepted_sources": [], "blocked_domains": []}

    monkeypatch.setattr(discovery_svc, "run_supervisor", _fake_supervisor)
    out = asyncio.run(discovery_svc.discover(
        "r1", _plan(), _search, None, _emit))
    assert out == []
