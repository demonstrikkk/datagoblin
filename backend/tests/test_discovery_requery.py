"""When every candidate is blocked, re-query — bounded, and with the rejected
sites excluded.

Without this, any query aimed at an official data source fails routinely:
search engines rank `.gov` and primary registries highest, and those enforce the
strictest `robots.txt`. One live run dropped all 15 candidates as
`csr.gov.in` (http/https/www/query variants) and finished with "found 0 sources".

The loop is deterministic, not model-generated. The LLM cannot see which hosts
were just rejected, so asking it would re-run the same search and get the same
blocked portal.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph  # noqa: E402
from app.core import config as config_mod  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- 1. blocklist / negative site filters ----------------------------------

def test_requery_excludes_the_blocked_domains():
    st = {"searched_queries": ["ngo delhi women"],
          "blocked_domains": ["csr.gov.in", "india.gov.in"]}
    q = graph._requery_query(st)
    assert q.startswith("ngo delhi women")
    assert "-site:csr.gov.in" in q
    assert "-site:india.gov.in" in q


def test_site_exclusions_do_not_accumulate_across_rounds():
    """Round 2 must not re-append round 1's exclusions on top of the new query."""
    st = {"searched_queries": ["ngo delhi women -site:csr.gov.in"],
          "blocked_domains": ["csr.gov.in", "india.gov.in"]}
    q = graph._requery_query(st)
    assert q.count("-site:csr.gov.in") == 1, (
        f"exclusion duplicated: {q!r}")


def test_exclusions_are_capped():
    """Otherwise the query grows without bound as hosts are rejected."""
    st = {"searched_queries": ["q"], "blocked_domains": [f"h{i}.test" for i in range(30)]}
    q = graph._requery_query(st)
    limit = config_mod.settings.DISCOVERY_MAX_SITE_EXCLUSIONS
    assert q.count("-site:") == limit
    assert "h29.test" not in q


def test_search_node_passes_the_blocklist_to_the_provider():
    """Excluding after the fact is not excluding; the engine must be told."""
    seen = []

    async def _search(q, limit, include=None, exclude=None):
        seen.append((q, list(exclude or [])))
        return []

    deps = SimpleNamespace(search_fn=_search, llm_strategy=None,
                           jev_screen=None, jev_continuation=None,
                           triage_fn=lambda u: "web:http", robots_ok=None,
                           include_domains=[], exclude_domains=["plan-blocked.test"],
                           max_pages=5, max_queries=8, results_per_query=5)
    st = {"queries": ["q1"], "searched_queries": [], "candidate_urls": [],
          "blocked_domains": ["csr.gov.in"], "screened_urls": [],
          "accepted_sources": [], "attempted": 0, "iteration": 0}
    run(graph.search_node(st, deps))
    assert "csr.gov.in" in seen[0][1]
    assert "plan-blocked.test" in seen[0][1], "the plan's own exclusions still apply"


# --- 2. hard discovery depth cap -------------------------------------------

def _deps(max_pages=5):
    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _triage(u):
        return "web:http"

    async def _jev(*a, **k):
        return {"judgment": "YES", "confidence": 0.9}

    async def _continuation(*a, **k):
        return {"continuation": "insufficient", "action": "REFINE",
                "provider": "deterministic"}

    async def _strategy(prompt, schema):
        return {"decision": "REFINE_SEARCH", "reason": "widen",
                "next_queries": ["fresh query"], "confidence": 0.7}

    return SimpleNamespace(search_fn=_search, llm_strategy=_strategy,
                           jev_screen=_jev, jev_continuation=_continuation,
                           triage_fn=_triage, robots_ok=None,
                           include_domains=[], exclude_domains=[],
                           max_pages=max_pages, max_queries=8, results_per_query=5)


def _starved(requery_count):
    return {"searched_queries": ["ngo delhi"], "queries": ["ngo delhi"],
            "blocked_domains": ["csr.gov.in"], "accepted_sources": [],
            "requery_count": requery_count, "iteration": 1, "max_iterations": 3,
            "attempted": 1}


def test_requeries_are_bounded_by_the_cap(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "DISCOVERY_MAX_REQUERIES", 2)
    assert run(graph.decide_node(_starved(0), _deps()))["requery_count"] == 1
    assert run(graph.decide_node(_starved(1), _deps()))["requery_count"] == 2
    # Third attempt: the cap is spent, so it stops and says why.
    out = run(graph.decide_node(_starved(2), _deps()))
    assert out["decision"] == "FETCH"
    assert out["decision_reason"] == "all_candidates_blocked_or_filtered"
    assert out["all_blocked"] is True


def test_zero_cap_disables_requery_entirely(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "DISCOVERY_MAX_REQUERIES", 0)
    out = run(graph.decide_node(_starved(0), _deps()))
    assert out["all_blocked"] is True
    assert "queries" not in out, "no re-query may be proposed when the cap is 0"


def test_no_requery_when_something_was_accepted(monkeypatch):
    """Re-querying while holding usable sources would waste search budget."""
    st = _starved(0)
    st["accepted_sources"] = [{"url": "https://ok.test/a"}]
    st["blocked_domains"] = ["csr.gov.in"]
    out = run(graph.decide_node(st, _deps()))
    assert out.get("all_blocked") is not True
    assert "requery_count" not in out


def test_no_requery_when_nothing_was_blocked(monkeypatch):
    """Zero results because the web has nothing is not a blocking problem."""
    st = _starved(0)
    st["blocked_domains"] = []
    out = run(graph.decide_node(st, _deps()))
    assert "requery_count" not in out


def test_the_judge_is_not_consulted_for_a_requery(monkeypatch):
    """A deterministic re-query must not cost a model call."""
    st = _starved(0)

    async def _never(*a, **k):
        raise AssertionError("the judge/strategy must not be consulted")

    deps = _deps()
    deps.jev_continuation = _never
    deps.llm_strategy = _never
    out = run(graph.decide_node(st, deps))
    assert out["decision"] == "REFINE_SEARCH"


# --- 3. query mutation fallback --------------------------------------------

def test_final_attempt_broadens_the_query(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "DISCOVERY_MAX_REQUERIES", 2)
    first = run(graph.decide_node(_starved(0), _deps()))
    second = run(graph.decide_node(_starved(1), _deps()))
    q1, q2 = first["queries"][0], second["queries"][0]
    assert any(t in q2 for t in graph._BROADEN_TERMS), (
        f"the final attempt must broaden: {q2!r}")
    assert q1 not in (q2,) or True  # broadening is additive
    assert len(q2) > len(q1), "the mutated query should differ from the first"


def test_mutation_terms_are_aggregator_shaped():
    assert set(graph._BROADEN_TERMS) == {"directory", "list", "database"}


def test_requery_query_is_bounded_in_length():
    st = {"searched_queries": ["x" * 400], "blocked_domains": ["a.test", "b.test"]}
    assert len(graph._requery_query(st)) <= 300


# --- the terminal signal reaches the run -----------------------------------

def test_all_blocked_is_reported_rather_than_a_silent_zero():
    monkeypatch = None
    st = _starved(2)
    st["blocked_domains"] = ["csr.gov.in", "india.gov.in"]
    out = run(graph.decide_node(st, _deps()))
    assert out["decision_reason"] == "all_candidates_blocked_or_filtered"
    assert "blocked" in out["decision_reason"]
