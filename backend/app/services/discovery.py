"""Discovery: supervisor-driven Tavily search + Jev-A screen + triage.

Thin orchestration over agents/graph.py (compiled LangGraph when installed,
identical manual stepping otherwise). Wires plan fields that were previously
dead: seed_domains/allowed_sources -> Tavily include_domains, max_pages caps.
Emits source.discovered with the caller's run_id (never blank).
"""
import datetime
from types import SimpleNamespace
from typing import Any

from app.agents.graph import run_supervisor
from app.core.config import settings
from app.core.logging import log
from app.providers.crawl import fetcher
from app.providers.decision import jev
from app.services.source_router import triage_source


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _domains(plan: dict) -> tuple[list[str], list[str]]:
    include = [str(d).strip().lstrip(".") for d in
               (plan.get("seed_domains", []) + plan.get("allowed_sources", []))]
    include = [d for d in include if d and "." in d and " " not in d][:50]
    return include, []


async def discover(run_id: str, plan: dict, search_fn: Any, llm: Any, emit: Any) -> list[dict]:
    requested = max(1, int(plan.get("requested_count", plan.get("max_results", 15))))
    max_pages = min(int(plan.get("max_pages", 12)), settings.RUN_MAX_PAGES)
    include_domains, exclude_domains = _domains(plan)

    async def _llm_strategy(prompt: str, schema: dict) -> dict:
        if llm is None:
            return {}
        try:
            out = await llm(prompt, schema)
        except Exception:
            return {}
        if isinstance(out, dict) and isinstance(out.get("data"), dict):
            return out["data"]
        return out if isinstance(out, dict) else {}

    async def _search_capped(query: str, limit: int,
                             include: list[str], exclude: list[str]) -> list[dict]:
        from app.core.errors import AppError
        capped = min(limit, settings.RUN_MAX_RESULTS_PER_QUERY)
        try:
            return await search_fn(query, capped, include, exclude)
        except TypeError:
            # Backwards-compatible search_fn(query, limit) without domain args;
            # a TypeError HERE propagates (genuine bug, not a signature probe).
            return await search_fn(query, capped)
        except AppError as e:
            if e.code == "E_PROVIDER_FATAL":
                return []  # auth/config: retrying is pointless (never retry 4xx/auth)
            raise  # transient: propagate so the loop retries next round (bounded)
        except Exception:
            raise  # transient: propagate for bounded loop-level retry

    async def _robots_ok(url: str) -> bool:
        """Is this URL crawlable at all? Cached per host for 1h by the fetcher.

        Checked during discovery rather than at fetch time, because a candidate
        we may not crawl is dead on arrival: one live run accepted two sources,
        both `robots-disallowed`, spent the whole budget, and returned 0 records
        with nothing to backfill from. Screening them here frees the slot for
        the next candidate instead.

        Placed BEFORE the Jev-A screen deliberately - the robots cache makes it
        nearly free, while a judge call is a real round trip to spend on a URL
        that was never going to be fetched.
        """
        getter = getattr(fetcher, "robots_allowed", None)
        if not callable(getter):
            return True  # no gate available => do not block the run
        try:
            allowed, _delay = await getter(url)
            return bool(allowed)
        except Exception as e:  # noqa: BLE001 (uncertain => allow; the fetch re-checks)
            log.info("robots check failed; allowing for the fetch to re-check",
                     extra={"data": {"url": url[:200], "error": str(e)[:120]}})
            return True

    deps = SimpleNamespace(
        search_fn=_search_capped,
        llm_strategy=_llm_strategy,
        jev_screen=jev.source_screening,
        jev_continuation=jev.research_continuation,
        robots_ok=_robots_ok,
        triage_fn=triage_source,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        max_pages=max_pages,
        max_queries=settings.SUPERVISOR_MAX_QUERIES,
        results_per_query=settings.RUN_MAX_RESULTS_PER_QUERY,
    )
    # Explicit seeds: user-requested URLs skip Jev-A screening (they were not
    # discovered) but face identical triage, fetch, extract, and evidence validation.
    seeds: list[dict] = []
    for s in plan.get("seed_urls", []) or []:
        url = ((s.get("url") if isinstance(s, dict) else s) or "").strip()
        if not url or url in {a["url"] for a in seeds}:
            continue
        seeds.append({"url": url,
                      "title": (s.get("title", "") if isinstance(s, dict) else "")[:300],
                      "snippet": "", "route": triage_source(url),
                      "screen_confidence": None, "seeded": True})
        if len(seeds) >= max_pages:
            break
    initial = {"run_id": run_id, "goal": plan.get("goal", ""), "entity": plan.get("entity", ""),
               "fields": plan.get("fields", []),
               "queries": list(plan.get("search_queries", [])[:5]),
               "searched_queries": [], "candidate_urls": [], "screened_urls": [],
               "accepted_sources": seeds, "extracted_records": [], "rejected_records": [],
               "requested_count": requested, "valid_count": 0, "missing_fields": [],
               "iteration": 0, "max_iterations": settings.SUPERVISOR_MAX_ITERATIONS,
               "last_searched": 1, "decision": "", "decision_reason": ""}
    final = await run_supervisor(initial, deps, run_id)
    accepted = list(final.get("accepted_sources", []))[:max_pages]
    # Surfaced, not swallowed: "every candidate was blocked and we re-queried
    # twice" is a materially different failure from "the web has nothing", and
    # the two call for different responses from the user.
    blocked = list(final.get("blocked_domains", []) or [])
    if not accepted and blocked:
        await emit({"type": "source.discovered", "run_id": run_id,
                    "stage": "DISCOVERING",
                    "message": (f"Found 0 sources: every candidate was blocked or "
                                f"filtered ({len(blocked)} site(s): "
                                f"{', '.join(blocked[:6])})"),
                    "progress": 25, "timestamp": _now(),
                    "data": {"count": 0, "blocked_domains": blocked[:50],
                             "all_candidates_blocked_or_filtered": True,
                             "requery_count": int(final.get("requery_count", 0) or 0)}})
        return accepted
    await emit({"type": "source.discovered", "run_id": run_id,
                "stage": "DISCOVERING", "message": f"Found {len(accepted)} sources",
                "progress": 25, "timestamp": _now(),
                "data": {"count": len(accepted),
                         "blocked_domains": blocked[:50]}})
    return accepted
