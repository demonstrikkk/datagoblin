"""Supervisor graph: SEARCH -> SCREEN -> DECIDE -> REFINE/FETCH|FINALIZE (<=max_iterations).

LangGraph moves state; Jev-D judges coverage; Runner executes. Nodes are pure
functions over (state, deps) and drive BOTH paths: the compiled LangGraph
(supervisor available) and the deterministic manual loop (langgraph absent) —
identical logic, zero duplication.

Node contract: return partial state dicts only (langgraph-fundamentals: never
mutate-and-return-full-state). List fields carry reducers (state.py).
"""
from functools import partial
from typing import Any, Literal
import asyncio

from app.agents.decisions import SupervisorDecision
from app.agents.policies import POLICIES
from app.core.config import settings


async def supervisor_step(state: dict, llm_strategy: Any, jev_continuation: Any) -> SupervisorDecision:
    """Jev-D coverage judgement first; then the LangChain schema-enforced strategy,
    then a raw llm_strategy fallback, then a deterministic break.

    Strict reader mode: the LangChain cloud rung (Groq/Gemini) never fires —
    llm_strategy (opencode) decides directly, so supervisor iterations cannot
    burn cloud quota either.

    Jev judges coverage and says whether more searching could plausibly help;
    it does not choose the next queries. That stays with the strategy model, and
    when neither is usable the deterministic policy breaks the loop rather than
    spinning to the iteration cap."""
    j = await jev_continuation(state.get("valid_count", 0),
                               max(1, state.get("requested_count", 20)),
                               state.get("missing_fields", []),
                               evidence_summary=state.get("evidence_summary", ""),
                               rounds_used=int(state.get("iteration", 0) or 0),
                               max_rounds=int(state.get("max_iterations", 0) or 0))
    if j.get("continuation") == "sufficient":
        return SupervisorDecision(decision="FETCH",
                                  reason=f"coverage sufficient ({j.get('provider', '?')})",
                                  missing_coverage=[], next_queries=[], confidence=0.9)
    if j.get("action") == "REVIEW":
        # A judged dead end. Surfacing it beats burning the remaining rounds on
        # searches the judge expects to find nothing.
        return SupervisorDecision(decision="FETCH",
                                  reason=f"stop and surface the coverage gap: {j.get('continuation')}",
                                  missing_coverage=list(state.get("missing_fields", [])),
                                  next_queries=[], confidence=0.6)
    prompt = (f"Goal: {state.get('goal','')}. Valid {state.get('valid_count',0)}/"
              f"{state.get('requested_count',20)}. Searched: {state.get('searched_queries',[])}. "
              f"Missing: {state.get('missing_fields',[])}. "
              "Return JSON {decision: REFINE_SEARCH|FETCH|FINALIZE, reason, "
              "missing_coverage[], next_queries[<=3], confidence}.")
    raw: dict = {}
    if not settings.OPENCODE_STRICT:
        try:  # LangChain structured output first (schema-native typed object)
            from app.providers.llm import langchain_client as lc
            out = await lc.decide(prompt, POLICIES["MAX_SEARCH_QUERIES"],
                                  state.get("searched_queries", []))
            if isinstance(out.get("data"), dict):
                raw = out["data"]
        except Exception:
            raw = {}
    if not raw:
        try:
            raw = await llm_strategy(prompt, SupervisorDecision.model_json_schema()) or {}
        except Exception:
            raw = {}
    try:
        allowed = set(SupervisorDecision.model_fields)
        dec = SupervisorDecision(**{**{"decision": "REFINE_SEARCH", "reason": "",
                                       "missing_coverage": [], "next_queries": [],
                                       "confidence": 0.0},
                                    **{k: v for k, v in raw.items() if k in allowed}})
    except Exception:
        dec = SupervisorDecision(decision="REFINE_SEARCH", reason="strategy fallback")
    used = state.get("searched_queries", [])
    return dec.validated({"max_queries": POLICIES["MAX_SEARCH_QUERIES"], "used_queries": used})


async def search_node(state: dict, deps: Any) -> dict:
    """Run pending queries (<=3 fresh, within query budget). Appends via reducers.

    Only successful queries are marked tried: transient failures stay eligible so
    the bounded loop retries them (loop-level retry keeps compiled and manual
    paths identical; a node-level RetryPolicy would diverge them).
    """
    tried = set(state.get("searched_queries", []))
    pending = [q for q in state.get("queries", []) if q not in tried][:3]
    room = max(0, deps.max_queries - len(tried))
    pending = pending[:room]
    found: list[dict] = []
    succeeded: list[str] = []
    for q in pending:
        try:
            results = await deps.search_fn(q, deps.results_per_query,
                                           deps.include_domains, deps.exclude_domains)
        except Exception:
            continue  # untried: eligible again next round (max_iterations bounds it)
        succeeded.append(q)
        found.extend([r for r in results if isinstance(r, dict)])
    return {"searched_queries": succeeded,
            "candidate_urls": found,
            "attempted": len(pending),
            "last_searched": len(succeeded),
            "iteration": int(state.get("iteration", 0)) + 1}


async def screen_node(state: dict, deps: Any) -> dict:
    """Jev-A screen on unscreened candidates; triage route attached. Single NO-path.

    Screens run in concurrent batches of 5 (the 282s-discovery fix: sequential
    Jev calls at ~10s each stalled whole runs). Decisions still apply in
    original order with the same cap, and every fresh URL is still marked
    screened. Batches stop once the cap fills, so at most one partial batch
    of calls is ever spent past the cap.
    """
    screened = set(state.get("screened_urls", []))
    fresh = [r for r in state.get("candidate_urls", [])
             if r.get("url") and r["url"] not in screened]
    base = len(state.get("accepted_sources", []))
    done = [r["url"] for r in fresh]
    accepted: list[dict] = []
    sem = asyncio.Semaphore(5)

    async def _one(r: dict) -> dict:
        async with sem:
            return await deps.jev_screen(r["url"], r.get("title", ""),
                                         r.get("snippet", ""), state.get("entity", ""))

    idx = 0
    while idx < len(fresh) and base + len(accepted) < deps.max_pages:
        batch = fresh[idx:idx + 5]
        idx += len(batch)
        for r, screen in zip(batch, await asyncio.gather(*(_one(r) for r in batch))):
            if base + len(accepted) >= deps.max_pages:
                break
            url = r["url"]
            if (screen or {}).get("judgment") == "NO":
                continue  # one path: any NO skips fetch+extract
            accepted.append({"url": url, "title": str(r.get("title", ""))[:300],
                             "snippet": str(r.get("snippet", ""))[:2000],
                             "route": deps.triage_fn(url),
                             "screen_confidence": (screen or {}).get("confidence", 0.0)})
    return {"screened_urls": done, "accepted_sources": accepted}


def _coverage_summary(state: dict) -> str:
    """What discovery actually knows, in one string, for the coverage judge.

    The judge was called with `valid_count` and `missing_fields`, neither of
    which any node has ever written - so it was always asked about 0 valid
    records out of 20 while holding no information at all. Records are not
    extracted until after this loop, so the honest signal here is source
    coverage, not record counts.
    """
    accepted = state.get("accepted_sources", []) or []
    tried = state.get("searched_queries", []) or []
    cands = state.get("candidate_urls", []) or []
    titles = [str(s.get("title", "")).strip() for s in accepted[:12]
              if str(s.get("title", "")).strip()]
    bits = [
        f"Search rounds completed: {int(state.get('iteration', 0) or 0)}",
        f"Queries run: {len(tried)}",
        f"Candidate URLs seen: {len(cands)}",
        f"Sources accepted for fetching: {len(accepted)}",
    ]
    if titles:
        bits.append("Accepted source titles: " + "; ".join(titles)[:800])
    return "\n".join(bits)


async def decide_node(state: dict, deps: Any) -> dict:
    """supervisor_step judgment -> validated decision fields. No fresh queries and
    no new ones proposed => FETCH (refining with nothing to search is waste)."""
    dec = await supervisor_step(state, deps.llm_strategy, deps.jev_continuation)
    update: dict = {"decision": dec.decision, "decision_reason": dec.reason[:500],
                    "evidence_summary": _coverage_summary(state)}
    if dec.decision == "REFINE_SEARCH" and dec.next_queries:
        update["queries"] = list(dec.next_queries)
    if update["decision"] == "REFINE_SEARCH" and not update.get("queries"):
        tried = set(state.get("searched_queries", []))
        if not [q for q in state.get("queries", []) if q not in tried]:
            update = {"decision": "FETCH", "decision_reason": "no fresh queries; proceed",
                      "evidence_summary": _coverage_summary(state)}
    return update


def _route(state: dict) -> Literal["refine", "fetch", "finish"]:
    if state.get("decision") == "FINALIZE":
        return "finish"
    if state.get("decision") == "FETCH":
        return "fetch"
    if int(state.get("iteration", 0)) >= int(state.get("max_iterations", 3)):
        return "fetch"  # budget exhausted => proceed with what exists (partial, honest)
    if not state.get("queries"):
        return "fetch"
    if state.get("attempted", 1) == 0:
        return "fetch"  # no fresh queries attempted => looping is waste; proceed
    return "refine"


def build_supervisor(deps: Any, checkpointer: Any = None) -> Any:
    """Compile search->screen->decide loop. None when langgraph is not installed."""
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError:
        return None
    from app.agents.state import ResearchState
    g = StateGraph(ResearchState)
    g.add_node("search", partial(search_node, deps=deps))
    g.add_node("screen", partial(screen_node, deps=deps))
    g.add_node("decide", partial(decide_node, deps=deps))
    g.add_edge(START, "search")
    g.add_edge("search", "screen")
    g.add_edge("screen", "decide")
    g.add_conditional_edges("decide", _route,
                            {"refine": "search", "fetch": END, "finish": END})
    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()


async def run_supervisor(initial: dict, deps: Any, run_id: str) -> dict:
    """Drive the loop. Compiled graph (InMemorySaver + thread_id=run_id) when
    langgraph is installed; otherwise step the identical node functions manually.
    Returns final state. Only ImportError falls back — real compile errors raise."""
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        graph = build_supervisor(deps, checkpointer=InMemorySaver())
    except ImportError:
        graph = None
    if graph is None:
        state = dict(initial)
        while True:
            state = {**state, **await search_node(state, deps)}
            state = {**state, **await screen_node(state, deps)}
            state = {**state, **await decide_node(state, deps)}
            if _route(state) != "refine":
                if state.get("decision") != "FINALIZE":
                    state["decision"] = "FETCH"
                return state
    config = {"configurable": {"thread_id": run_id or "supervisor"}}
    return await graph.ainvoke(initial, config)
