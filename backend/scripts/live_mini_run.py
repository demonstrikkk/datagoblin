"""One small LIVE run: real Tavily + Gemini + fetch + validate. Hard budget:
1 search query, 3 pages max, no retries beyond code defaults. Prints summary only."""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.providers.crawl import fetcher  # noqa: E402
from app.providers.llm import generate as llm_mod  # noqa: E402
from app.providers.search import tavily as tavily_mod  # noqa: E402
from app.services import impersonation as impersonation_svc  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402
from app.services import runner as runner_svc  # noqa: E402


async def main():
    plan, provider = await planner_svc.compile_plan(
        "Find 3 AI startups in London with founders and funding", llm_mod.structured_generate)
    plan["search_queries"] = plan["search_queries"][:1]
    plan["max_pages"] = 3
    plan["requested_count"] = 3
    print(f"plan via {provider}: entity={plan['entity']} queries={plan['search_queries']}",
          flush=True)
    run_id = str(uuid.uuid4())
    events = []

    async def _emit(ev):
        events.append(ev)
        print(f"  evt {ev.get('type')} {ev.get('stage')} :: {ev.get('message','')[:90]}",
              flush=True)

    store_box = {}

    async def _store(rid, pl, rows, counts):
        store_box.update(dataset_id=str(uuid.uuid4()), rows=rows, counts=counts)
        return store_box["dataset_id"]

    async def _persist(source):
        pass

    async def _fetch(url, method):
        if method == "crawl4ai":
            return await fetcher.crawl4ai_fetch(url)
        if method == "impersonate":
            return await impersonation_svc.impersonate_fetch(url)
        return await fetcher.http_fetch(url)

    ctx = {"search": tavily_mod.search, "fetch": _fetch, "llm": llm_mod.structured_generate,
           "store": _store, "persist_source": _persist, "cancelled": lambda: False}
    try:
        result = await asyncio.wait_for(
            runner_svc.execute_run(run_id, plan, ctx, _emit), timeout=420)
    except asyncio.TimeoutError:
        result = {"status": "TIMEOUT", "note": "420s watchdog; partial events kept"}
    print(f"result={result} counts={store_box.get('counts')}", flush=True)
    for r in store_box.get("rows", [])[:3]:
        started = {k: (v.get("value"), v.get("verification_status"))
                   for k, v in r.get("fields", {}).items()}
        print("row:", started)


if __name__ == "__main__":
    asyncio.run(main())
