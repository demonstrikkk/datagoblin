"""Agentic pipeline deep checks (langgraph-fundamentals methodology).

No network, no keys. Proves: compile-first discipline, reducers accumulate,
partial-update nodes, conditional REFINE loop (multi-round), failed-search retry,
manual-fallback equivalence, checkpointer history + thread isolation, streaming.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph as graph_mod  # noqa: E402
from app.agents.state import ResearchState  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.services.source_router import triage_source  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _deps(results=None, llm=None, fail_queries=()):
    results = results if results is not None else []
    calls = []

    async def _search(q, limit, include=None, exclude=None):
        calls.append(q)
        if q in fail_queries and calls.count(q) == 1:
            raise RuntimeError("transient blip")
        return [dict(r) for r in results if r.get("q", "q1") == q][:limit]

    return SimpleNamespace(search_fn=_search, llm_strategy=llm,
                           jev_screen=jev.source_screening,
                           jev_continuation=jev.research_continuation,
                           triage_fn=triage_source, include_domains=[], exclude_domains=[],
                           max_pages=10, max_queries=8, results_per_query=5,
                           _calls=calls)


def _initial(queries=("q1",)):
    return {"run_id": "r-ag", "goal": "g", "entity": "Acme AI", "fields": [],
            "queries": list(queries), "searched_queries": [], "candidate_urls": [],
            "screened_urls": [], "accepted_sources": [], "extracted_records": [],
            "rejected_records": [], "requested_count": 5, "valid_count": 0,
            "missing_fields": [], "iteration": 0, "max_iterations": 3,
            "last_searched": 1, "attempted": 1, "decision": "", "decision_reason": ""}


def _rec(url, title="Acme AI raises", snippet="Acme AI funding round", q="q1"):
    return {"url": url, "title": title, "snippet": snippet, "q": q}


# -- versions + compile discipline ------------------------------------------------
def test_versions_present():
    from importlib.metadata import version
    assert version("langgraph") and version("langchain-core")


def test_build_compiles_here():
    assert graph_mod.build_supervisor(_deps()) is not None  # guards silent fallback


# -- node discipline: partial updates only, declared keys only --------------------
def test_nodes_return_partial_declared_updates():
    declared = set(ResearchState.__annotations__)
    deps = _deps([_rec("https://a.example/1")])
    st = _initial()
    for node in (graph_mod.search_node, graph_mod.screen_node, graph_mod.decide_node):
        out = run(node(dict(st), deps))
        assert set(out) <= declared, f"{node.__name__} leaks {set(out) - declared}"
        st = {**st, **out}


# -- reducers accumulate across rounds ---------------------------------------------
def test_multi_round_refine_loop():
    async def _llm(prompt, schema):
        if "q2" not in prompt:
            return {"decision": "REFINE_SEARCH", "reason": "need more",
                    "missing_coverage": [], "next_queries": ["q2"], "confidence": 0.8}
        return {}
    deps = _deps([_rec("https://a.example/1", q="q1"), _rec("https://b.example/2", q="q2")],
                 llm=_llm)
    final = run(graph_mod.run_supervisor(_initial(), deps, "r-multi"))
    assert final["searched_queries"] == ["q1", "q2"]  # accumulated, not overwritten
    assert final["iteration"] == 2
    assert {a["url"] for a in final["accepted_sources"]} == \
        {"https://a.example/1", "https://b.example/2"}


# -- failed search is retried, not burned ---------------------------------------------
def test_transient_failure_retried_then_succeeds():
    deps = _deps([_rec("https://a.example/1")], fail_queries=("q1",))
    final = run(graph_mod.run_supervisor(_initial(), deps, "r-retry"))
    assert final["searched_queries"] == ["q1"]  # marked only on success
    assert len(final["accepted_sources"]) == 1
    assert deps._calls.count("q1") == 2  # attempted twice, bounded by max_iterations


# -- manual fallback equivalence (langgraph absent) --------------------------------------
def test_manual_path_matches_compiled(monkeypatch):
    results = [_rec("https://a.example/1"), _rec("https://b.example/2")]
    compiled = run(graph_mod.run_supervisor(_initial(), _deps(results), "r-eq"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.memory", None)
    monkeypatch.setattr(graph_mod, "build_supervisor",
                        lambda *a, **k: (_ for _ in ()).throw(ImportError("absent")))
    manual = run(graph_mod.run_supervisor(_initial(), _deps(results), "r-eq"))
    assert [a["url"] for a in manual["accepted_sources"]] == \
           [a["url"] for a in compiled["accepted_sources"]]
    assert manual["iteration"] == compiled["iteration"]


# -- checkpointer history + thread isolation -----------------------------------------------
def test_checkpointer_history_and_isolation():
    from langgraph.checkpoint.memory import InMemorySaver
    deps = _deps([_rec("https://a.example/1")])
    g = graph_mod.build_supervisor(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-hist"}}
    final = run(g.ainvoke(_initial(), cfg))
    assert final["iteration"] >= 1

    async def _hist():
        return [s async for s in g.aget_state_history(cfg)]
    hist = run(_hist())
    assert len(hist) >= 2  # time-travel/debuggability holds

    other = run(g.ainvoke(_initial(("q9",)), {"configurable": {"thread_id": "t-other"}}))
    assert other["accepted_sources"] == [] and other["searched_queries"] == ["q9"]


# -- streaming -----------------------------------------------------------------------
def test_stream_updates():
    from langgraph.checkpoint.memory import InMemorySaver
    deps = _deps([_rec("https://a.example/1")])
    g = graph_mod.build_supervisor(deps, checkpointer=InMemorySaver())

    async def _collect():
        return [c async for c in g.astream(
            _initial(), {"configurable": {"thread_id": "t-stream"}}, stream_mode="updates")]
    chunks = run(_collect())
    assert chunks, "stream must yield node updates"
    seen = {node for c in chunks for node in c}
    assert {"search", "screen", "decide"} <= seen
