"""Supervisor tools — deterministic shims over real adapters. Read-only; no DB writes."""
from typing import Any

from app.agents.decisions import SupervisorDecision


async def search_web(search_fn: Any, query: str, limit: int = 5) -> list[dict]:
    """Thin shim so the graph never touches the SDK directly (injectable seam)."""
    return await search_fn(query, min(limit, 5))


def inspect_search_results(results: list[dict], entity: str) -> dict:
    urls: list[str] = []
    for r in results or []:
        u = str(r.get("url", ""))
        if u.startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
    return {"count": len(urls), "urls": urls[:20], "entity": entity}


def assess_coverage(valid: int, requested: int) -> dict:
    return {"valid": valid, "requested": max(1, requested),
            "sufficient": valid >= requested, "gap": max(0, requested - valid)}


def apply_decision(decision: SupervisorDecision, state: dict) -> dict:
    """Translate a validated decision into partial state updates (Runner executes)."""
    if decision.decision == "REFINE_SEARCH":
        return {"queries": list(decision.next_queries),
                "decision": "REFINE_SEARCH", "decision_reason": decision.reason[:500]}
    if decision.decision == "FINALIZE":
        return {"decision": "FINALIZE", "decision_reason": decision.reason[:500]}
    return {"decision": "FETCH", "decision_reason": decision.reason[:500]}
