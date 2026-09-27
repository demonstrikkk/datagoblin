"""The coverage judge must actually judge.

`research_continuation` was a pure function of `valid >= requested` with no
model call at all, while `graph.py` documented it as the coverage judge - and
it was invoked with `valid_count`/`missing_fields`, neither of which any node
ever wrote, so it only ever saw "0 valid out of 20". Counting is necessary but
not sufficient: 3-of-5 with no leads left should stop, 4-of-5 missing a required
field should continue, and neither is visible to a count.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.decision import jev  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _judge(cont, action, conf=0.8):
    async def _call(state, questions, **kw):
        assert "continuation" in questions and "action" in questions
        return {"continuation": {"type": "choice", "choice": cont,
                                 "probabilities": {cont: conf, "other": 1 - conf}},
                "action": {"type": "choice", "choice": action}}
    return _call


# --- it is a real judge now -------------------------------------------------

def test_judge_is_consulted_and_its_answer_is_used(monkeypatch):
    seen = {}
    inner = _judge("sufficient", "FETCH")

    async def _call(state, questions, **kw):
        seen["state"] = state
        return await inner(state, questions)

    monkeypatch.setattr(jev, "_jev_call", _call)
    out = run(jev.research_continuation(3, 5))
    assert out["provider"] == "jev"
    assert out["continuation"] == "sufficient"
    assert "3" in seen["state"] and "5" in seen["state"]


def test_judge_can_stop_a_search_a_count_would_keep_going(monkeypatch):
    """3 of 5 with no leads left: the count says REFINE, the judge says stop."""
    monkeypatch.setattr(jev, "_jev_call", _judge("sufficient", "FETCH"))
    out = run(jev.research_continuation(3, 5,
                                        evidence_summary="3 queries, 2 sources, no new leads"))
    assert out["continuation"] == "sufficient"
    assert out["action"] == "FETCH"
    assert out["fallback"] == "insufficient", "the count-based answer is kept for comparison"


def test_judge_can_ask_for_more_when_the_target_is_plausible(monkeypatch):
    monkeypatch.setattr(jev, "_jev_call", _judge("insufficient", "REFINE"))
    out = run(jev.research_continuation(4, 5, missing_fields=["revenue"]))
    assert out["continuation"] == "insufficient"
    assert out["action"] == "REFINE"


def test_judge_can_surface_a_dead_end_for_review(monkeypatch):
    monkeypatch.setattr(jev, "_jev_call", _judge("insufficient", "REVIEW"))
    out = run(jev.research_continuation(1, 10))
    assert out["action"] == "REVIEW"


def test_judge_state_carries_the_context_it_needs(monkeypatch):
    seen = {}
    base = _judge("uncertain", "REFINE")

    async def _call(state, questions, **kw):
        seen["state"] = state
        return await base(state, questions)

    monkeypatch.setattr(jev, "_jev_call", _call)
    run(jev.research_continuation(2, 8, missing_fields=["revenue", "hq"],
                                  evidence_summary="sources: A, B",
                                  rounds_used=2, max_rounds=3))
    s = seen["state"]
    assert "revenue" in s and "hq" in s
    assert "2 of 3" in s
    assert "sources: A, B" in s


# --- the deterministic policy is still the safety net ----------------------

def test_no_judge_falls_back_to_the_count(no_judge):
    out = run(jev.research_continuation(5, 5))
    assert out == {"continuation": "sufficient", "action": "FETCH",
                   "provider": "deterministic"}


def test_no_judge_keeps_refining_when_short(no_judge):
    out = run(jev.research_continuation(1, 5))
    assert out["provider"] == "deterministic"
    assert out["action"] == "REFINE"


def test_no_judge_nothing_collected_refines(no_judge):
    out = run(jev.research_continuation(0, 5))
    assert out["continuation"] == "insufficient" and out["action"] == "REFINE"


def test_zero_target_is_not_asked(no_judge):
    out = run(jev.research_continuation(0, 0))
    assert out["provider"] == "deterministic"


def test_malformed_judge_answer_falls_back(monkeypatch):
    async def _garbage(state, questions, **kw):
        return {"continuation": {"type": "noul", "noul": 0.7}}

    monkeypatch.setattr(jev, "_jev_call", _garbage)
    out = run(jev.research_continuation(5, 5))
    assert out["provider"] == "deterministic"
    assert out["continuation"] == "sufficient"


def test_judge_never_invents_fetch_for_an_insufficient_result(monkeypatch):
    """A continuation without a usable action must not default to FETCH."""
    async def _partial(state, questions, **kw):
        return {"continuation": {"type": "choice", "choice": "insufficient",
                                 "probabilities": {"insufficient": 0.7}}}

    monkeypatch.setattr(jev, "_jev_call", _partial)
    out = run(jev.research_continuation(1, 5))
    assert out["continuation"] == "insufficient"
    assert out["action"] == "REFINE", "must not silently proceed on insufficient evidence"


# --- the supervisor honours the judgement ----------------------------------

def test_supervisor_stops_on_a_judged_dead_end(monkeypatch):
    from app.agents import graph

    async def _jev(valid, requested, missing=None, **kw):
        return {"continuation": "insufficient", "action": "REVIEW", "provider": "jev"}

    async def _never(prompt, schema):
        raise AssertionError("the strategy model must not be consulted")

    dec = run(graph.supervisor_step(
        {"goal": "g", "valid_count": 1, "requested_count": 10,
         "missing_fields": ["revenue"], "iteration": 2, "max_iterations": 3},
        _never, _jev))
    assert dec.decision == "FETCH", "stop searching, proceed with the gap surfaced"
    assert dec.next_queries == []
    assert "coverage gap" in dec.reason


def test_supervisor_asks_for_more_when_the_judge_says_so(monkeypatch):
    from app.agents import graph

    async def _jev(valid, requested, missing=None, **kw):
        return {"continuation": "insufficient", "action": "REFINE", "provider": "jev"}

    async def _strategy(prompt, schema):
        return {"decision": "REFINE_SEARCH", "reason": "widen the net",
                "next_queries": ["acme funding 2024"], "confidence": 0.7}

    dec = run(graph.supervisor_step(
        {"goal": "g", "valid_count": 1, "requested_count": 10}, _strategy, _jev))
    assert dec.decision == "REFINE_SEARCH"
    assert dec.next_queries == ["acme funding 2024"]


def test_coverage_summary_reports_real_progress(monkeypatch):
    from app.agents import graph
    s = graph._coverage_summary({
        "iteration": 2, "searched_queries": ["a", "b", "c"],
        "candidate_urls": [{"url": "1"}, {"url": "2"}],
        "accepted_sources": [{"url": "1", "title": "Acme funding"},
                             {"url": "2", "title": ""}]})
    assert "Search rounds completed: 2" in s
    assert "Queries run: 3" in s
    assert "Candidate URLs seen: 2" in s
    # Both sources were accepted; only one carried a usable title, and the
    # summary reports them separately so the judge is not misled by blanks.
    assert "Sources accepted for fetching: 2" in s
    assert "Acme funding" in s
    assert ";" not in s.split("Accepted source titles: ")[1].strip(" ;"), \
        "an untitled source must not appear as an empty title"
