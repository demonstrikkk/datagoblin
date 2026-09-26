"""DATAGOBLIN FastAPI control plane — 11 endpoints per docs/15, SSE per docs/16.

Wiring: planner/discovery/crawler/extractor/validator/normalizer/deduper Runner
+ Tavily/Gemini-Groq providers + Jev decisions + Supabase|local-dev repository
+ capped SSE bus. No mocks: unavailable subsystems raise named errors.
"""
import asyncio
import datetime
import json
import uuid

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from app.api.deps import correlation_id, require_api_key
from app.core.config import settings
from app.core.constants import EVENT_TYPES
from app.core.errors import AppError, not_found, validation
from app.core.logging import log, set_level
from app.events.stream import bus
from app.providers.crawl import fetcher
from app.providers.llm import generate as llm_provider
from app.providers.search import tavily as search_provider
from app.repositories.factory import build_repo
from app.schemas.run import DatasetView, Envelope, RunView
from app.services import crawler as crawler_svc
from app.services import exporter as exporter_svc
from app.services import impersonation as impersonation_svc
from app.services import jobs as jobs_svc
from app.services import metering as metering_svc
from app.services import planner as planner_svc
from app.services import runner as runner_svc

set_level(settings.LOG_LEVEL)

app = FastAPI(title="DATAGOBLIN API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list(),
                   allow_methods=["GET", "POST"], allow_headers=["*"])

PLANS: dict[str, dict] = {}
RUNS: dict[str, dict] = {}
TASKS: dict[str, asyncio.Task] = {}
REPO: object = None

#: Bound on in-memory registries. History/datasets live in the repository;
#: these dicts are live-run state only. Oldest terminal runs evicted first;
#: active runs are never evicted.
MAX_KEPT_RUNS = 200
_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def _evict_registries() -> None:
    for rid, task in list(TASKS.items()):
        if task.done():
            TASKS.pop(rid, None)
    while len(PLANS) > MAX_KEPT_RUNS:
        PLANS.pop(next(iter(PLANS)), None)
    overflow = [rid for rid, r in RUNS.items() if r.get("status") in _TERMINAL]
    while len(RUNS) > MAX_KEPT_RUNS and overflow:
        rid = overflow.pop(0)
        RUNS.pop(rid, None)
        task = TASKS.pop(rid, None)
        if task is not None and not task.done():
            task.cancel()
        bus.evict(rid)


def repo() -> object:
    assert REPO is not None, "repository not initialised"
    return REPO


async def _fetch_method(url: str, method: str) -> dict:
    """Single fetch dispatch shared by runs and map (same waterfall)."""
    if method == "crawl4ai":
        return await fetcher.crawl4ai_fetch(url)
    if method == "impersonate":
        return await impersonation_svc.impersonate_fetch(url)
    if method == "jina":
        return await fetcher.jina_fetch(url)
    return await fetcher.http_fetch(url)


@app.on_event("startup")
async def _startup() -> None:
    global REPO
    REPO = build_repo()
    missing = [n for n in ("TAVILY_API_KEY", "GEMINI_API_KEY") if not getattr(settings, n)]
    if missing and settings.REQUIRE_KEYS_AT_STARTUP:
        log.info("startup keys missing", extra={"data": {"missing": missing}})
    log.info("startup complete")


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in TASKS.values():
        t.cancel()
    await fetcher.aclose_pool()


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.http, content=exc.envelope())


def _run_view(run_id: str) -> dict:
    r = RUNS.get(run_id, {})
    return {"run_id": run_id, "status": r.get("status", "UNKNOWN"),
            "current_stage": r.get("current_stage", ""), "progress": r.get("progress", 0),
            "counters": r.get("counters", {}), "dataset_id": r.get("dataset_id"),
            "error": r.get("error", "")}


# -- compile ---------------------------------------------------------------
@app.post("/api/workflows/compile", dependencies=[Depends(require_api_key)])
async def compile_plan(body: dict, cid: str = Depends(correlation_id)) -> dict:
    async def _llm(prompt: str, schema: dict) -> dict:
        return await llm_provider.structured_generate(prompt, schema)
    plan, provider = await planner_svc.compile_plan(body.get("prompt", ""), _llm)
    seeds = []
    for s in (body.get("seed_urls", []) or [])[:12]:
        url = (s.get("url") if isinstance(s, dict) else s) or ""
        url = url.strip() if isinstance(url, str) else ""
        if url:
            seeds.append(url)
    if seeds:
        plan = {**plan, "seed_urls": seeds}
        planner_svc.coerce_plan(plan)  # re-validate merged plan (strings only)
    pid = str(uuid.uuid4())
    PLANS[pid] = plan
    log.info("plan compiled", extra={"data": {"plan_id": pid, "provider": provider}})
    return Envelope[dict](data={"plan_id": pid, "plan": plan, "provider": provider},
                          meta={"correlation_id": cid}).model_dump()


# -- runs ------------------------------------------------------------------
@app.post("/api/runs", dependencies=[Depends(require_api_key)])
async def start_run(body: dict, cid: str = Depends(correlation_id)) -> dict:
    plan_id = body.get("plan_id", "")
    base = PLANS.get(plan_id)
    if base is None:
        raise validation(f"Unknown plan_id: {plan_id[:36]}")
    plan = dict(base)
    # Batch support: caller-supplied seed URLs (+ optional credit budget) merge
    # into a plan COPY (stored plans are never mutated).
    seeds = []
    for s in (body.get("seed_urls", []) or [])[:12]:
        url = (s.get("url") if isinstance(s, dict) else s) or ""
        url = url.strip() if isinstance(url, str) else ""
        if url:
            seeds.append(url)
    if seeds:
        plan["seed_urls"] = seeds
    if body.get("credit_budget"):
        try:
            plan["credit_budget"] = max(0, int(body["credit_budget"]))
        except (TypeError, ValueError):
            raise validation("credit_budget must be an integer >= 0")
    try:
        planner_svc.coerce_plan(plan)
    except Exception as e:
        raise validation(str(e))
    run_id = str(uuid.uuid4())
    RUNS[run_id] = {"status": "PLANNING", "current_stage": "PLANNING", "progress": 2,
                    "counters": {}, "dataset_id": None, "error": ""}
    r = repo()
    r.create_run(run_id, plan_id, plan)

    async def _emit(ev: dict) -> None:
        ev = {**ev, "run_id": run_id}
        if ev.get("type") not in EVENT_TYPES:
            ev["type"] = "stage.progress"
        await bus.emit(run_id, ev)
        r.append_event(run_id, ev)
        st = RUNS.get(run_id)
        if st is not None:
            st.update(current_stage=ev.get("stage", st.get("current_stage", "")),
                      progress=ev.get("progress", st.get("progress", 0)))
            if ev["type"] == "run.completed":
                data = ev.get("data", {}) or {}
                st.update(status="COMPLETED", dataset_id=data.get("dataset_id"),
                          counters={"attempted": data.get("attempted", 0),
                                    "successful": data.get("successful", 0),
                                    "failed": data.get("failed", 0),
                                    "records": data.get("records", 0),
                                    "verified": data.get("verified", 0),
                                    "needs_review": data.get("needs_review", 0)})
            elif ev["type"] == "run.failed":
                st.update(status="FAILED", error=ev.get("message", ""))
            elif ev["type"] == "run.cancelled":
                st.update(status="CANCELLED")

    async def _fetch(url: str, method: str) -> dict:
        return await _fetch_method(url, method)

    async def _llm(prompt: str, schema: dict) -> dict:
        return await llm_provider.structured_generate(prompt, schema)

    async def _store(rid: str, pl: dict, rows: list[dict], counts: dict) -> str:
        return r.finalize_dataset(rid, pl, rows, counts)

    async def _persist_source(source: dict) -> None:
        r.upsert_source(run_id, source)
        fp = source.get("fingerprint", "")
        if fp:
            r.note_fingerprint(run_id, fp, source.get("url", ""))

    def _record_charge(entry: dict) -> None:
        r.record_charge(run_id, entry)

    ctx = {"search": search_provider.search, "fetch": _fetch, "llm": _llm,
           "store": _store, "persist_source": _persist_source,
           "record_charge": _record_charge,
           "cancelled": lambda: RUNS.get(run_id, {}).get("status") == "CANCELLED"}
    TASKS[run_id] = asyncio.create_task(runner_svc.execute_run(run_id, plan, ctx, _emit))
    _evict_registries()
    return Envelope[dict](data={"run_id": run_id, "status": "PLANNING"},
                          meta={"correlation_id": cid}).model_dump()


# -- jobs + map ---------------------------------------------------------------
@app.get("/api/jobs/{run_id}")
async def job_status(run_id: str, skip: int = Query(default=0, ge=0),
                     limit: int = Query(default=100, ge=1, le=500),
                     cid: str = Depends(correlation_id)) -> dict:
    st = jobs_svc.get_job_status(repo(), run_id, skip, limit, memory=RUNS.get(run_id))
    if st is None:
        raise not_found("job", run_id)
    return {"data": st, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/map", dependencies=[Depends(require_api_key)])
async def map_url(body: dict, cid: str = Depends(correlation_id)) -> dict:
    """URL discovery without extraction: fetch once, return all links + 1 credit."""
    from app.services.source_router import triage_source
    url = (body.get("url", "") or "").strip()
    try:
        route = triage_source(url)
    except AppError:
        raise validation(f"Bad map URL: {url[:120]}")
    link_limit = max(1, min(int(body.get("limit", 200) or 200), 500))
    try:
        page = await crawler_svc._one(url, route, _fetch_method, asyncio.Semaphore(1))
    except AppError as e:
        raise e
    if page.get("skipped"):
        links: list[str] = []
    else:
        links = fetcher.extract_all_links(page.get("html", ""), page.get("final_url", url),
                                          limit=link_limit)
    job_id = f"map-{uuid.uuid4().hex[:8]}"
    entry = metering_svc.Ledger().bill(job_id, "map", 1)
    try:
        repo().record_charge(job_id, entry)
    except Exception as e:  # noqa: BLE001 (map result matters more than its receipt)
        log.info("map ledger persist failed", extra={"data": {"error": str(e)[:150]}})
    return {"data": {"url": url, "final_url": page.get("final_url", url),
                     "method": page.get("method", ""),
                     "skipped": page.get("skipped", ""),
                     "links": links, "count": len(links),
                     "credits_used": entry["credits"]},
            "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/runs/{run_id}")
async def run_status(run_id: str, cid: str = Depends(correlation_id)) -> dict:
    if run_id not in RUNS:
        stored = repo().get_run(run_id)
        if stored is None:
            raise not_found("run", run_id)
        return {"data": stored, "error": None, "meta": {"correlation_id": cid}}
    view = RunView(run_id=run_id, status=_run_view(run_id)["status"],
                   current_stage=_run_view(run_id)["current_stage"],
                   progress=_run_view(run_id)["progress"],
                   counters=_run_view(run_id).get("counters", {}),
                   dataset_id=_run_view(run_id)["dataset_id"],
                   error=_run_view(run_id)["error"])
    return {"data": view.model_dump(), "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(require_api_key)])
async def cancel_run(run_id: str, cid: str = Depends(correlation_id)) -> dict:
    st = RUNS.get(run_id)
    if st is None:
        raise not_found("run", run_id)
    st["status"] = "CANCELLED"
    task = TASKS.pop(run_id, None)
    if task is not None:
        task.cancel()
    _evict_registries()
    return {"data": {"run_id": run_id, "status": "CANCELLED"},
            "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request):
    async def _gen():
        idx = 0
        while True:
            if await request.is_disconnected():
                break
            events, total, done = await bus.snapshot(run_id, idx)
            for ev in events:
                yield {"event": ev.get("type", "message"), "data": json.dumps(ev, default=str)}
                idx += 1
            if done or (RUNS.get(run_id, {}).get("status") in ("COMPLETED", "FAILED", "CANCELLED") and idx >= total):
                break
            await asyncio.sleep(0.5)
    return EventSourceResponse(_gen(), ping=settings.SSE_PING_S)


# -- datasets --------------------------------------------------------------
@app.get("/api/datasets")
async def list_datasets(cid: str = Depends(correlation_id)) -> dict:
    items = []
    for d in repo().list_datasets():
        items.append(DatasetView(id=d.get("id", ""), run_id=d.get("run_id", ""),
                                 name=d.get("name", ""),
                                 schema=d.get("schema", d.get("schema_json", [])),
                                 record_count=d.get("record_count", 0),
                                 counts=d.get("counts", {}),
                                 created_at=d.get("created_at", "")).model_dump(by_alias=True))
    return {"data": items, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}")
async def get_dataset(did: str, cid: str = Depends(correlation_id)) -> dict:
    ds = repo().get_dataset(did)
    if not ds:
        raise not_found("dataset", did)
    view = DatasetView(id=ds.get("id", did), run_id=ds.get("run_id", ""),
                       name=ds.get("name", ""),
                       schema=ds.get("schema", ds.get("schema_json", [])),
                       record_count=ds.get("record_count", len(ds.get("records", []))),
                       counts=ds.get("counts", {}),
                       created_at=ds.get("created_at", "")).model_dump(by_alias=True)
    view["records"] = ds.get("records", [])
    return {"data": view, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/records")
async def get_records(did: str, q: str = "", limit: int = Query(default=100, ge=1, le=500),
                      offset: int = Query(default=0, ge=0),
                      cid: str = Depends(correlation_id)) -> dict:
    out = repo().get_records(did, q[:200], limit, offset)
    if not out:
        raise not_found("dataset", did)
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/sources")
async def get_sources(did: str, cid: str = Depends(correlation_id)) -> dict:
    out = repo().get_sources(did)
    if not out:
        raise not_found("dataset", did)
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/export", dependencies=[Depends(require_api_key)])
async def export_dataset(did: str, body: dict, cid: str = Depends(correlation_id)) -> dict:
    ds = repo().get_dataset(did)
    if not ds:
        raise not_found("dataset", did)
    fmt, content, filename = exporter_svc.export_dataset(
        ds.get("schema", ds.get("schema_json", [])), ds.get("records", []),
        body.get("format", "json"),
        [c.strip() for c in settings.EXPORT_FIELDS.split(",") if c.strip()] or None,
        {"name": ds.get("name", did), "run_id": ds.get("run_id", did),
         "counts": ds.get("counts", {})})
    try:
        export_id = repo().save_export(did, fmt, len(content.encode("utf-8")))
        persisted: bool | str = True
    except Exception as e:  # noqa: BLE001 (download must survive persist failure; labeled)
        export_id, persisted = "", f"persist failed: {str(e)[:150]}"
    # Phase-5: each export call is billed work (charge key includes the export
    # row, so repeat exports bill per call while retries stay idempotent).
    credits = 0
    try:
        run_id = ds.get("run_id", did)
        prior = [e for e in repo().ledger(run_id) if e.get("stage") == "export"]
        entry = metering_svc.Ledger().bill(run_id, "export", 1, seq=len(prior) + 1)
        repo().record_charge(run_id, entry)
        credits = entry["credits"]
    except Exception as e:  # noqa: BLE001 (download must survive billing failure)
        log.info("export ledger persist failed", extra={"data": {"error": str(e)[:150]}})
    meta = {"correlation_id": cid, "export_id": export_id, "persisted": persisted,
            "credits_used": credits}
    if fmt == "csv":
        return PlainTextResponse(content, media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={filename}"})
    if fmt == "md":
        return PlainTextResponse(content, media_type="text/markdown",
                                 headers={"Content-Disposition": f"attachment; filename={filename}"})
    return {"data": {"format": fmt, "content": content}, "error": None, "meta": meta}


@app.get("/api/history")
async def history(cid: str = Depends(correlation_id)) -> dict:
    return {"data": repo().run_history(), "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/health")
async def health() -> dict:
    return {"data": {"status": "ok", "version": "0.1.0",
                     "time": datetime.datetime.utcnow().isoformat() + "Z"},
            "error": None, "meta": {}}
