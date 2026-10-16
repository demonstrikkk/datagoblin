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


class DiscoveryModelDown(RuntimeError):
    """The model server died mid-run. Raised, never swallowed, so the runner's
    terminal guard turns it into a FAILED run naming the cause — instead of the
    supervisor grinding through every iteration at full timeout cost each."""


#: How many consecutive model-call *exceptions* (not empty results) trip the
#: breaker. Two in a row is already a pattern — one is weather — and each dead
#: call costs its full timeout, so the count stays low on purpose.
_MODEL_FAILURE_TRIP = 3

#: When a previously-seen host is excluded outright for refusing us. All three
#: must hold, and the hit-rate ceiling is the strictest of them: a host that
#: blocks us but also serves us is having a bad day, not refusing us, and
#: excluding it would discard a source that has produced records before.
BLOCK_MIN_BLOCKS = 3
BLOCK_MIN_VISITS = 4
BLOCK_MAX_HIT_RATE = 0.25

#: How much a known-good host can outrank a textually identical unknown one.
#: Capped rather than unbounded because the prior is a prior: a host that was
#: excellent six months ago should not outrank the question itself.
HOST_TRUST_WEIGHT = 1.5


def _domains(plan: dict) -> tuple[list[str], list[str]]:
    include = [str(d).strip().lstrip(".") for d in
               (plan.get("seed_domains", []) + plan.get("allowed_sources", []))]
    include = [d for d in include if d and "." in d and " " not in d][:50]
    return include, []


#: Fewer distinct hosts than this in the accepted set triggers top-up searches.
DIVERSITY_FLOOR_HOSTS = 4

#: Top-up searches per discovery, hard cap. Each is one Tavily call plus
#: screens — bounded and cheap next to a supervisor round.
DIVERSITY_TOP_UPS = 2


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


async def _diversify(run_id: str, plan: dict, accepted: list[dict],
                     max_pages: int, search_capped, robots_ok, emit) -> list[dict]:
    """Top the accepted set up to the host-diversity floor, in place order."""
    from urllib.parse import urlparse

    def _hosts(rows):
        return {h for h in (_host_of(r.get("url", "")) for r in rows) if h}

    hosts = _hosts(accepted)
    if len(hosts) >= DIVERSITY_FLOOR_HOSTS or len(accepted) >= max_pages:
        return accepted
    queries = list(plan.get("search_queries", [])[:5]) or [str(plan.get("goal", ""))]
    tried = 0
    for q in queries:
        if len(hosts) >= DIVERSITY_FLOOR_HOSTS or len(accepted) >= max_pages:
            break
        if tried >= DIVERSITY_TOP_UPS:
            break
        tried += 1
        try:
            results = await search_capped(
                q, settings.RUN_MAX_RESULTS_PER_QUERY, [], sorted(hosts))
        except Exception:  # noqa: BLE001 (one dead search must not sink the set)
            continue
        fresh = 0
        for r in results or []:
            url = str((r or {}).get("url") or "")
            h = _host_of(url)
            if not url or not h or h in hosts:
                continue
            try:
                if not await robots_ok(url):
                    continue
            except Exception:  # noqa: BLE001
                pass
            accepted.append({"url": url,
                             "title": str((r or {}).get("title") or "")[:300],
                             "snippet": str((r or {}).get("snippet") or "")[:500],
                             "route": triage_source(url)})
            hosts.add(h)
            fresh += 1
            if len(hosts) >= DIVERSITY_FLOOR_HOSTS or len(accepted) >= max_pages:
                break
        try:
            await emit({"type": "source.discovered", "run_id": run_id,
                        "stage": "DISCOVERING",
                        "message": f"Diversity top-up: {fresh} new host(s), "
                                   f"{len(hosts)} hosts total",
                        "progress": 25, "timestamp": _now(),
                        "data": {"count": len(accepted),
                                 "hosts": sorted(hosts)}})
        except Exception:  # noqa: BLE001 (emit is best-effort)
            pass
    return accepted[:max_pages]


def blocked_hosts_from_prior(prior: dict[str, dict],
                             already: set[str]) -> set[str]:
    """Hosts this instance has learned to skip, from past runs.

    A host that has blocked us repeatedly is excluded before it can cost a
    search result, a robots fetch and a judge call. `blocked_domains` inside
    the supervisor was per-run only and filled from robots failures observed
    *within that run*, so nothing carried across runs: the same portal that
    refused us last week was re-tried today.

    All three conditions must hold, and the hit-rate ceiling is the strictest of
    them. A host that blocks us but also serves us is having a bad day, not
    refusing us, and excluding it would discard a source that has produced
    verified records before. Only `visit >= ` matters for the floor: one
    unlucky 403 must never write a host off.
    """
    out: set[str] = set()
    for host, info in (prior or {}).items():
        h = str(host or "").strip().lower()
        if not h or h in already:
            continue
        visits = int(info.get("visits") or 0)
        blocks = int(info.get("block_count") or 0)
        hits = int(info.get("successes") or 0)
        if (blocks >= BLOCK_MIN_BLOCKS and visits >= BLOCK_MIN_VISITS
                and hits / max(1, visits) <= BLOCK_MAX_HIT_RATE):
            out.add(h)
            log.info("excluding host that has repeatedly refused us",
                     extra={"data": {"host": h, "visits": visits,
                                     "blocks": blocks, "successes": hits}})
    return out


async def discover(run_id: str, plan: dict, search_fn: Any, llm: Any, emit: Any,
                   host_prior: Any = None) -> list[dict]:
    """Find candidate sources. `host_prior` is an optional `async () -> dict`
    returning `{host: {trust, known, block_count, ...}}` from past runs.

    Discovery used to hold no data handle at all, so `source_reputation` and
    `yieldmap` existed and informed nothing here: the same host blocked last
    week was re-tried today, and a host with a 0.95 verified-cell rate was
    ranked the same as one with 0.02. The literature is clear that seed
    quality predicts crawl yield, so this is the cheapest available accuracy.
    """
    requested = max(1, int(plan.get("requested_count", plan.get("max_results", 15))))
    max_pages = min(int(plan.get("max_pages", 12)), settings.RUN_MAX_PAGES)
    include_domains, exclude_domains = _domains(plan)

    prior: dict[str, dict] = {}
    if callable(host_prior):
        try:
            prior = await host_prior() or {}
        except Exception as e:  # noqa: BLE001 (cold start is a valid state)
            log.info("host prior unavailable; discovery runs cold",
                     extra={"data": {"error": str(e)[:120]}})
            prior = {}
    if prior:
        log.info("discovery using host prior",
                 extra={"data": {"hosts_known": len(prior)}})

    # A host that has blocked us repeatedly is excluded before it can cost a
    # search result, a robots fetch and a judge call. `blocked_domains` inside
    # the supervisor was per-run only and filled from robots failures within
    # that run, so nothing carried across runs. The threshold is deliberately
    # high and requires a poor hit rate, so a host that 403s twice by accident
    # is not written off.
    already_excluded = {str(d).lower() for d in (exclude_domains or [])}
    already_excluded |= blocked_hosts_from_prior(prior, already_excluded)
    if already_excluded:
        exclude_domains = sorted(already_excluded)[:150]

    # The provider died between the run preflight and this stage. The preflight
    # is a point check at run start; a server that dies a minute later leaves
    # every discovery round-trip hanging on full timeouts with no error
    # surfacing — the observed shape was minutes of spinner in DISCOVERING and
    # then a user cancel. One cheap probe here fails the run in ~5s with the
    # cause named, instead of burning the whole discovery budget finding out.
    #
    # Only when the chain has nowhere else to go: with OPENCODE_STRICT off,
    # Groq/Gemini can still serve, and the per-call cascade already handles a
    # dead primary by falling over to them.
    if llm is not None and settings.OPENCODE_STRICT:
        from app.providers.llm import opencode as opencode_llm
        try:
            state = await opencode_llm.available()
        except Exception:  # noqa: BLE001 (a broken probe must not fail a run)
            state = {}
        if state and not state.get("reachable") and state.get("error") not in (
                "disabled", "unconfigured"):
            raise DiscoveryModelDown(
                "the LLM server stopped responding before discovery "
                f"({state.get('base_url')}: "
                f"{state.get('error') or 'unreachable'}). Start it and re-run — "
                "nothing was spent.")

    # Consecutive model-call exceptions trip the breaker; empty-but-valid
    # results do not. A dead provider mid-discovery otherwise costs its full
    # timeout on every strategy call, every screen, every round — each round
    # minutes long, with the only backstop a wall-clock cap set in minutes.
    failures = 0

    async def _llm_strategy(prompt: str, schema: dict) -> dict:
        nonlocal failures
        if llm is None:
            return {}
        try:
            out = await llm(prompt, schema)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures >= _MODEL_FAILURE_TRIP:
                raise DiscoveryModelDown(
                    "the LLM server stopped responding during discovery "
                    f"({failures} consecutive call failures). Start it and "
                    "re-run — pages already stored are kept.") from exc
            return {}
        failures = 0
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
        host_prior=prior,
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

    # Host-diversity floor: caps alone cannot produce it. Twelve pages from two
    # hosts is a "success" no cap prevents — and it is what a news-flavoured
    # query plus robots-filtered alternatives produces. So when the accepted
    # set spans fewer than four hosts, run up to two more searches that EXCLUDE
    # the accepted hosts, forcing the engine off the dominant domains. Same
    # robots gate and triage as the main loop; a top-up that surfaces nothing
    # crawlable simply adds nothing.
    try:
        accepted = await _diversify(
            run_id, plan, accepted, max_pages, _search_capped,
            _robots_ok, emit)
    except Exception:  # noqa: BLE001 (diversity is additive; never sink discovery)
        log.info("diversity top-up failed; keeping supervisor sources",
                 extra={"data": {"run_id": run_id}})
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
