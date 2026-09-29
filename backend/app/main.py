"""DATAGOBLIN FastAPI control plane — 11 endpoints per docs/15, SSE per docs/16.

Wiring: planner/discovery/crawler/extractor/validator/normalizer/deduper Runner
+ Tavily/Gemini-Groq providers + Jev decisions + Supabase|local-dev repository
+ capped SSE bus. No mocks: unavailable subsystems raise named errors.
"""
import asyncio
import datetime
import hmac
import json
import uuid

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
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
from app.repositories import factory as factory_repo
from app.repositories.factory import build_repo
from app.schemas.run import DatasetView, Envelope, RunView
from app.services import backfill as backfill_svc
from app.services import crawler as crawler_svc
from app.services import coverage as coverage_svc
from app.services import exporter as exporter_svc
from app.services import impersonation as impersonation_svc
from app.services import jobs as jobs_svc
from app.services import metering as metering_svc
from app.services import planner as planner_svc
from app.services import proposals as proposals_svc
from app.services import runner as runner_svc
from app.services import selector_learn as selector_learn_svc
from app.services import selectors as selectors_svc
from app.services import sqlq as sqlq_svc

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
#: PARTIAL belongs here: a budget-stopped run is finished and persisted, and it
#: used to be omitted. Because eviction only ever picks from this list, a run
#: that hit its credit or runtime cap was retained forever while COMPLETED runs
#: were retired — the one status most likely to repeat in a long session was the
#: one that leaked.
_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "PARTIAL")


def _id_arg(kind: str, value: str) -> str:
    """Reject a malformed resource id as invalid input, not as an outage.

    Both repositories key on `uuid`. Without this, a value like `not-an-id`
    reaches Postgres, which fails the cast, and `dependency()` maps that to a
    503 E_DEPENDENCY — telling the caller the database is broken when the
    request was simply nonsense. A bad id is a 422.
    """
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise validation(f"Malformed {kind} id: expected a UUID")
    return value


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
    # A run lived only as an asyncio task on this process, so a crash or a
    # restart left its row mid-flight forever and /api/jobs reported a ghost
    # indistinguishable from a healthy run. Fail them honestly instead.
    recover = getattr(REPO, "recover_orphan_runs", None)
    if callable(recover):
        try:
            recover()
        except Exception as e:  # noqa: BLE001 (recovery must never block startup)
            log.warning("orphan run recovery failed",
                        extra={"data": {"error": str(e)[:150]}})
    log.info("startup complete", extra={"data": {"persistence": factory_repo.ACTIVE}})


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in TASKS.values():
        t.cancel()
    await fetcher.aclose_pool()


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.http, content=exc.envelope())


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Validation errors in the same envelope as everything else.

    FastAPI's default is `{"detail": [...]}`, a different shape from the
    `{data, error, meta}` every other response uses. The frontend reads
    `payload.error.message`, so a 422 produced a blank error box - the request
    just looked like it had failed silently.
    """
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
    message = first.get("msg", "invalid request")
    if loc:
        message = f"{loc}: {message}"
    return JSONResponse(status_code=422, content={
        "data": None,
        "error": {"code": "E_VALIDATION",
                  "message": message,
                  "details": {"fields": [
                      {"field": ".".join(str(x) for x in e.get("loc", [])
                                         if x not in ("body", "query")),
                       "message": e.get("msg", "")}
                      for e in exc.errors()[:20]]}},
        "meta": {}})


def _run_view(run_id: str) -> dict:
    r = RUNS.get(run_id, {})
    return {"run_id": run_id, "status": r.get("status", "UNKNOWN"),
            "current_stage": r.get("current_stage", ""), "progress": r.get("progress", 0),
            "counters": r.get("counters", {}), "dataset_id": r.get("dataset_id"),
            # Whether this run finished everything. A PARTIAL run stored real
            # records but did not complete, and the flag is what says so.
            "partial": bool(r.get("partial", False)),
            # Why a zero-record run found nothing. A bare "0 records" is
            # indistinguishable from a broken app.
            "no_yield_reason": r.get("no_yield_reason", ""),
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
    # Persisted as well as cached, so the id still resolves after a restart.
    # A store that cannot hold plans is not fatal - the in-memory copy serves
    # this process - but it must be said out loud rather than swallowed.
    saver = getattr(repo(), "save_plan", None)
    if callable(saver):
        try:
            saver(pid, plan)
        except Exception as e:  # noqa: BLE001
            log.warning("plan not persisted; it will not survive a restart",
                        extra={"data": {"plan_id": pid, "error": str(e)[:150]}})
    log.info("plan compiled", extra={"data": {"plan_id": pid, "provider": provider}})
    return Envelope[dict](data={"plan_id": pid, "plan": plan, "provider": provider},
                          meta={"correlation_id": cid}).model_dump()


# -- runs ------------------------------------------------------------------
@app.post("/api/runs", dependencies=[Depends(require_api_key)])
async def start_run(body: dict, cid: str = Depends(correlation_id)) -> dict:
    plan_id = body.get("plan_id", "")
    base = PLANS.get(plan_id)
    if base is None:
        # Fall back to the store: a plan compiled before a restart is still a
        # valid plan, and refusing it would force the caller to recompile for
        # no reason.
        getter = getattr(repo(), "get_plan", None)
        if callable(getter):
            try:
                base = getter(plan_id)
            except Exception as e:  # noqa: BLE001
                log.warning("plan lookup failed",
                            extra={"data": {"plan_id": plan_id[:36], "error": str(e)[:150]}})
                base = None
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
    # Reuse is on by default, which means a URL already fetched is not fetched
    # again. This is the escape hatch for "I know it changed, go look": without
    # it, once a URL has been seen it could never be refreshed and a changed
    # page would stay stale with no way out short of a new database.
    if "reuse_stored_pages" in body:
        plan["reuse_stored_pages"] = bool(body["reuse_stored_pages"])
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
            data = ev.get("data", {}) or {}
            counters = {"attempted": data.get("attempted", 0),
                        "successful": data.get("successful", 0),
                        "failed": data.get("failed", 0),
                        "records": data.get("records", 0),
                        # Named for what they measure. `verified`/`needs_review`
                        # conflated records with fields and counted unjudged
                        # claims as verified.
                        "records_fully_verified": data.get("records_fully_verified", 0),
                        "records_needing_review": data.get("records_needing_review", 0),
                        "fields_verified": data.get("fields_verified", 0),
                        "fields_judgment_unavailable": data.get(
                            "fields_judgment_unavailable", 0),
                        "fields_rate_limited": data.get("fields_rate_limited", 0)}
            if ev["type"] == "run.completed":
                st.update(status="COMPLETED", dataset_id=data.get("dataset_id"),
                          partial=False,
                          no_yield_reason=data.get("no_yield_reason", ""),
                          counters=counters)
            elif ev["type"] == "run.partial":
                # A budget-stopped run stores what it earned. It was not handled
                # here, so a partial run sat at its initial status forever and
                # the UI showed a finished-looking run still "PLANNING".
                st.update(status="PARTIAL", dataset_id=data.get("dataset_id") or None,
                          partial=True, error=data.get("partial_reason")
                          or ev.get("message", ""), counters=counters)
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

    async def _persist_page(page: dict) -> str:
        """Store the crawled page so evidence can cite it later.

        Sync psycopg on the event loop: a single indexed upsert per settled
        page, bounded by RUN_MAX_PAGES. Offloaded to a thread so a slow write
        cannot stall the crawl's worker loop.
        """
        return await asyncio.to_thread(r.upsert_page, run_id, page)

    async def _reuse(url: str) -> dict | None:
        """Stored evidence for a URL this instance has already fetched, or None.

        The fingerprint write side has been live all along (`_persist_source`
        notes every settled URL; the table holds 101 rows across 14 runs). What
        was missing was the read, so every re-run re-fetched URLs it already
        had in hand and paid for them again.

        Two gates, both required. The fingerprint says we have seen this exact
        request before; the stored page is what we would have to reuse. If the
        fingerprint is known but no page survived — runs are deleted by cascade,
        so a page can outlive neither its run nor its fingerprint — the answer
        is None and the URL is fetched for real. Treating "seen" as "reusable"
        would drop the page and quietly shrink the dataset.
        """
        fp = politeness_svc.fingerprint("GET", url)
        if not fp:
            return None
        if not await asyncio.to_thread(r.seen_fingerprint, fp):
            return None
        return await asyncio.to_thread(r.find_page_by_url, url, True)

    def _record_charge(entry: dict) -> None:
        r.record_charge(run_id, entry)

    ctx = {"search": search_provider.search, "fetch": _fetch, "llm": _llm,
           "store": _store, "persist_source": _persist_source,
           "persist_page": _persist_page, "reuse": _reuse,
           "record_charge": _record_charge,
           "cancelled": lambda: RUNS.get(run_id, {}).get("status") == "CANCELLED"}
    TASKS[run_id] = asyncio.create_task(runner_svc.execute_run(run_id, plan, ctx, _emit))
    _evict_registries()
    return Envelope[dict](data={"run_id": run_id, "status": "PLANNING"},
                          meta={"correlation_id": cid}).model_dump()


# -- jobs + map ---------------------------------------------------------------
@app.get("/api/jobs/{run_id}", dependencies=[Depends(require_api_key)])
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


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_api_key)])
async def run_status(run_id: str, cid: str = Depends(correlation_id)) -> dict:
    if run_id not in RUNS:
        stored = await asyncio.to_thread(repo().get_run, run_id)
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
async def stream(run_id: str, request: Request, key: str | None = Query(default=None)):
    """Server-sent events for one run.

    Auth: `EventSource` cannot send a custom header, so the key is accepted as
    a query parameter. That is a deliberate trade — a key in a URL can end up in
    access logs, which is a worse property than a header, but leaving this
    endpoint open exposes the full run log and the run ids that reach it. The
    ids are unguessable UUIDs, so gating here is defence in depth rather than
    the only control; the enumeration endpoints that leak them are gated too.
    """
    await require_api_key(key)

    async def _gen():
        idx = 0
        while True:
            if await request.is_disconnected():
                break
            events, total, done = await bus.snapshot(run_id, idx)
            for ev in events:
                yield {"event": ev.get("type", "message"), "data": json.dumps(ev, default=str)}
                idx += 1
            # PARTIAL was missing here. A budget-stopped run is persisted as
            # FAILED + partial=true with status PARTIAL, so this loop never
            # exited: the connection stayed open polling a finished run, and
            # the client was left waiting on a socket that had nothing left to
            # send. The client also self-closes, but the server-side generator
            # was still holding a slot.
            if done or (RUNS.get(run_id, {}).get("status") in
                        ("COMPLETED", "FAILED", "CANCELLED", "PARTIAL") and idx >= total):
                break
            await asyncio.sleep(0.5)
    return EventSourceResponse(_gen(), ping=settings.SSE_PING_S)


# -- datasets --------------------------------------------------------------
@app.get("/api/datasets", dependencies=[Depends(require_api_key)])
async def list_datasets(cid: str = Depends(correlation_id)) -> dict:
    items = []
    for d in await asyncio.to_thread(repo().list_datasets):
        items.append(DatasetView(id=d.get("id", ""), run_id=d.get("run_id", ""),
                                 name=d.get("name", ""),
                                 schema=d.get("schema", d.get("schema_json", [])),
                                 record_count=d.get("record_count", 0),
                                 counts=d.get("counts", {}),
                                 created_at=d.get("created_at", "")).model_dump(by_alias=True))
    return {"data": items, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}", dependencies=[Depends(require_api_key)])
async def get_dataset(did: str, cid: str = Depends(correlation_id)) -> dict:
    ds = await asyncio.to_thread(repo().get_dataset, did)
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


@app.get("/api/datasets/{did}/records", dependencies=[Depends(require_api_key)])
async def get_records(did: str, q: str = "", limit: int = Query(default=100, ge=1, le=500),
                      offset: int = Query(default=0, ge=0),
                      cid: str = Depends(correlation_id)) -> dict:
    out = await asyncio.to_thread(repo().get_records, did, q[:200], limit, offset)
    if not out:
        raise not_found("dataset", did)
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/sources", dependencies=[Depends(require_api_key)])
async def get_sources(did: str, cid: str = Depends(correlation_id)) -> dict:
    out = await asyncio.to_thread(repo().get_sources, did)
    if not out:
        raise not_found("dataset", did)
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/propose", dependencies=[Depends(require_api_key)])
async def propose_change(did: str, body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Read a sentence and say what it would do. Executes nothing.

    The whole point of asking in words is not having to know which endpoint
    fills a gap you noticed — but the answer must never be a model writing into
    a dataset. So this returns an intent, the existing endpoint that will carry
    it out, and the cost, for the user to approve separately.
    """
    question = str(body.get("question") or "").strip()
    if not question:
        raise validation("say what you want asked or changed")
    r = repo()
    if await asyncio.to_thread(r.get_dataset_schema, did) is None:
        raise not_found("dataset", did)
    try:
        out = proposals_svc.propose(r, question, did)
    except ValueError as exc:
        raise validation(str(exc))
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/backfill", dependencies=[Depends(require_api_key)])
async def backfill_proposal(did: str, fields: str = "",
                            limit_pages: int = Query(default=8, ge=1, le=20),
                            cid: str = Depends(correlation_id)) -> dict:
    """What a backfill would cover, and what it would cost. Reads only.

    Only fields with no value on any record are offered. A field that some
    records carry is a coverage gap, and widening this to partial fields would
    quietly change what backfill is allowed to rewrite.
    """
    r = repo()
    if await asyncio.to_thread(r.get_dataset_schema, did) is None:
        raise not_found("dataset", did)
    try:
        out = backfill_svc.propose(
            r, did, [f for f in fields.split(",") if f.strip()] or None,
            limit_pages)
    except backfill_svc.BackfillRefused as exc:
        raise validation(str(exc))
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/backfill", dependencies=[Depends(require_api_key)])
async def backfill_run(did: str, body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Fill entirely-absent fields from pages this run already stored.

    `apply: false` is the default and is a real dry run: the extraction and the
    verification both execute, and only the writing is withheld, so the numbers
    in the proposal can be checked against what actually happens.

    Nothing is crawled. A backfilled value carries its own quote and verdict, or
    it is not written.
    """
    r = repo()
    if await asyncio.to_thread(r.get_dataset_schema, did) is None:
        raise not_found("dataset", did)
    fields = [str(f) for f in (body.get("fields") or []) if str(f).strip()]
    apply_flag = bool(body.get("apply"))
    # Bounded in the route, not clamped: this number decides how many stored
    # pages are re-read, and a typo that silently became 1 would make a backfill
    # report success while quietly doing a twentieth of the work.
    limit_pages = int(body.get("limit_pages") or 8)
    if not 1 <= limit_pages <= 20:
        raise validation("limit_pages must be between 1 and 20")

    async def _llm(prompt: str, schema: dict) -> dict:
        return await llm_provider.structured_generate(prompt, schema)

    try:
        out = await backfill_svc.run(r, did, fields or None, llm=_llm,
                                     limit_pages=limit_pages, apply=apply_flag)
    except backfill_svc.BackfillRefused as exc:
        raise validation(str(exc))
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/query", dependencies=[Depends(require_api_key)])
async def query_dataset(did: str, body: dict,
                        cid: str = Depends(correlation_id)) -> dict:
    """Answer a question about one dataset by running the SQL a model wrote.

    The model proposes; it does not execute. The statement is checked against a
    grammar that only knows this dataset's columns, then wrapped so the result
    is row-capped, then run inside a read-only transaction that is rolled back.
    A rejected statement comes back as a 422 naming the offending word, so the
    caller can retry with a valid one.

    The SQL is returned alongside the rows. A question you cannot inspect the
    query for is a question you have to take on faith.
    """
    question = str(body.get("question") or "").strip()
    limit = int(body.get("limit") or 200)
    if not question:
        raise validation("question is required")
    if len(question) > 2000:
        raise validation("question too long (2000 chars max)")
    limit = max(1, min(limit, 1000))

    schema = await asyncio.to_thread(repo().get_dataset_schema, did)
    if schema is None:
        raise not_found("dataset", did)
    total = (await asyncio.to_thread(repo().get_records, did, "", 1, 0)).get("total", 0)

    relation, columns = sqlq_svc.relation_sql(did, schema)
    prompt = sqlq_svc.build_prompt(question, schema, total, sqlq_svc.DEFAULT_EXAMPLES)

    async def _attempt(extra: str = "") -> tuple[str, dict]:
        try:
            out = await llm_provider.structured_generate(prompt + extra, {"type": "object"})
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise dependency(f"could not reach the model: {str(exc)[:120]}")
        return sqlq_svc.extract_sql(out.get("data", out) if isinstance(out, dict) else out), out

    raw, _ = await _attempt()
    if not raw:
        raise dependency("the model did not return a query")

    # One informed retry. A rejected statement is usually the right idea written
    # with a construct the grammar does not allow, and saying so to the model is
    # cheaper than showing the user a 422 for a question they phrased normally.
    try:
        safe = sqlq_svc.validate(raw, columns)
    except ValueError as first:
        retry_sql, _ = await _attempt(
            f"\n\nYour previous SQL was rejected: {first}\n"
            f"Rewrite it using only the columns listed above. Use OR to combine "
            f"conditions; do not use ANY, ARRAY, or functions not listed.")
        if not retry_sql:
            raise validation(f"{first}", details={"sql": raw})
        try:
            safe = sqlq_svc.validate(retry_sql, columns)
        except ValueError as second:
            raise validation(f"{second}", details={"sql": retry_sql})

    sql = sqlq_svc.build_query(safe.replace("FROM records", f"FROM ({relation}) AS records"),
                               relation, limit)
    try:
        rows = await asyncio.to_thread(repo().run_readonly_sql, sql)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise dependency(f"the query could not be executed: {str(exc)[:160]}")

    return {"data": {"dataset_id": did, "question": question,
                     "sql": safe, "executed_sql": sql,
                     "columns": list(rows[0].keys()) if rows else columns[:1],
                     "row_count": len(rows), "truncated": len(rows) >= limit,
                     "rows": rows},
            "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/workflows/refine", dependencies=[Depends(require_api_key)])
async def refine_plan(body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Change a compiled plan by instruction, and show exactly what moved.

    Re-planning from scratch on every edit throws away the rest of the plan: a
    second pass that adds one field came back with a different entity, a
    different count, and different queries. This applies the change to the plan
    in hand, recompiles it to check it is still valid, and returns a field-level
    diff so a change to one thing is visible as a change to one thing.

    Compiles, but does not start a run. Nothing is crawled until someone asks.
    """
    plan_id = str(body.get("plan_id") or "").strip()
    instruction = str(body.get("instruction") or "").strip()
    if not plan_id or not instruction:
        raise validation("plan_id and instruction are both required")
    if len(instruction) > 2000:
        raise validation("instruction too long (2000 chars max)")

    current = PLANS.get(plan_id) or await asyncio.to_thread(repo().get_plan, plan_id)
    if not current:
        raise not_found("plan", plan_id)

    def _fingerprint(plan: dict) -> dict:
        return {f.get("name", ""): f for f in (plan.get("fields") or [])}

    before = _fingerprint(current)
    goal = f"{current.get('goal', '')} Refinement: {instruction}"
    revised, provider = await planner_svc.compile_plan(goal, llm_provider.structured_generate)
    planner_svc.coerce_plan(revised)

    after = _fingerprint(revised)
    added = [n for n in after if n not in before]
    removed = [n for n in before if n not in after]
    changed = [n for n in after if n in before and after[n] != before[n]]

    if not (added or removed or changed):
        raise validation(
            f"The refinement produced the same plan. Rephrase it — nothing about "
            f"{instruction[:80]!r} was understood as a change.")

    merged = dict(revised)
    merged["goal"] = current.get("goal") or revised.get("goal", "")
    merged["refined_from"] = plan_id
    for key in ("seed_urls", "allowed_sources"):
        if current.get(key):
            merged[key] = current[key]

    new_id = str(uuid.uuid4())
    PLANS[new_id] = merged
    saver = getattr(repo(), "save_plan", None)
    if callable(saver):
        try:
            await asyncio.to_thread(saver, new_id, merged)
        except Exception as e:  # noqa: BLE001
            log.warning("refined plan not persisted",
                        extra={"data": {"plan_id": new_id, "error": str(e)[:150]}})
    return {"data": {"plan_id": new_id, "plan": merged, "provider": provider,
                     "diff": {"added": added, "removed": removed, "changed": changed}},
            "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/selectors", dependencies=[Depends(require_api_key)])
async def list_selectors(cid: str = Depends(correlation_id)) -> dict:
    """Checked-in domain schemas, each with whether it still loads.

    `valid: false` means the file is on disk but no longer passes
    shape-validation, so extraction is silently skipping it. Reporting the
    count without that flag is how a dead schema looks like a live one.
    """
    return {"data": selector_learn_svc.list_schemas(),
            "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/selectors/propose", dependencies=[Depends(require_api_key)])
async def propose_selectors(body: dict,
                            cid: str = Depends(correlation_id)) -> dict:
    """Learn CSS selectors for a domain, verified against a real page.

    Nothing is saved. The response carries, per field, the text each selector
    actually produced, so the decision to keep a schema is made by reading a
    sample rather than by trusting a CSS string.
    """
    url = str(body.get("url") or "").strip()
    fields = body.get("fields") or []
    if not url:
        raise validation("url is required")
    if not isinstance(fields, list) or not fields:
        raise validation("fields is required: a list of {name, type}")

    html = ""
    page_id = ""
    stored_id = str(body.get("page_id") or "").strip()
    if stored_id:
        # Reuse the page the run already stored. Re-fetching would cost another
        # request and could learn from a different rendering than the one
        # extraction will actually see.
        page = await asyncio.to_thread(repo().get_page, stored_id)
        if not page:
            raise not_found("page", stored_id)
        html = page.get("raw_html") or ""
        if not html:
            raise validation(f"page {stored_id} stored no raw_html to learn from")
        page_id = stored_id

    try:
        draft = await selector_learn_svc.propose(url, fields, html=html, page_id=page_id)
    except ValueError as exc:
        raise validation(str(exc))
    return {"data": draft, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/selectors/save", dependencies=[Depends(require_api_key)])
async def save_selectors(body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Check in a verified draft.

    Refuses to replace an existing schema without `overwrite: true`, because
    silently clobbering a working selector set makes a domain regress with no
    record that it ever worked.
    """
    try:
        saved = selector_learn_svc.save(body, overwrite=bool(body.get("overwrite")))
    except ValueError as exc:
        raise validation(str(exc))
    return {"data": saved, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/conflicts", dependencies=[Depends(require_api_key)])
async def list_conflicts(did: str, cid: str = Depends(correlation_id)) -> dict:
    """Every disputed cell in the dataset, with both sides and their quotes.

    The UI could already count conflicting cells and stop there, which is how a
    dataset ended up reporting "8 conflicting" as a headline nobody could act
    on. The rivals the deduper preserved at merge time are what make each of
    those eight a decidable question instead of a number.
    """
    schema = await asyncio.to_thread(repo().get_dataset_schema, did)
    if schema is None:
        raise not_found("dataset", did)
    rows = (await asyncio.to_thread(repo().get_records, did, "", settings.EXPORT_MAX_ROWS, 0)).get("records", [])
    conflicts = coverage_svc.collect_conflicts(rows)
    return {"data": {"dataset_id": did, "schema": schema,
                     "conflicts": conflicts,
                     "open": sum(1 for c in conflicts if not c["decided"])},
            "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/conflicts/resolve",
          dependencies=[Depends(require_api_key)])
async def resolve_conflict(did: str, body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Record a human decision about one disputed cell.

    Two moves, and neither writes a value that was not extracted: keep the
    incumbent, or adopt a rival that came from a different source. There is no
    free-text path, because a dataset whose claim is "every value carries its
    evidence" cannot accept an unquoted one.
    """
    if await asyncio.to_thread(repo().get_dataset_schema, did) is None:
        raise not_found("dataset", did)
    record_id = str(body.get("record_id") or "")
    field = str(body.get("field") or "")
    choice = str(body.get("choice") or "")
    if not record_id or not field:
        raise validation("record_id and field are required")
    if choice not in ("keep", "adopt"):
        raise validation("choice must be 'keep' or 'adopt'")

    rows = (await asyncio.to_thread(repo().get_records, did, "", settings.EXPORT_MAX_ROWS, 0)).get("records", [])
    target = next((r for r in rows if str(r.get("record_id")) == record_id), None)
    if target is None:
        raise not_found("record", record_id)
    cell = (target.get("fields") or {}).get(field)
    try:
        updated = coverage_svc.apply_resolution(cell, choice, body.get("rival_index"))
    except (ValueError, TypeError) as exc:
        raise validation(str(exc))
    if not await asyncio.to_thread(repo().update_record_cell, did, record_id, field, updated):
        raise not_found("record", record_id)
    return {"data": {"dataset_id": did, "record_id": record_id, "field": field,
                     "cell": updated},
            "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/datasets/{did}/coverage", dependencies=[Depends(require_api_key)])
async def dataset_coverage(did: str, cid: str = Depends(correlation_id)) -> dict:
    """Fill/verdict counts per field, plus the ranked backlog of what is missing.

    Derived purely by reading stored records. A declared field that no page ever
    carried is reported as absent, never defaulted — a coverage number that
    filled its own gaps would be worse than no number at all.
    """
    schema = await asyncio.to_thread(repo().get_dataset_schema, did)
    if schema is None:
        raise not_found("dataset", did)
    rows = (await asyncio.to_thread(repo().get_records, did, "", settings.EXPORT_MAX_ROWS, 0)).get("records", [])
    matrix = coverage_svc.field_coverage(rows, schema)
    conflicts = coverage_svc.collect_conflicts(rows)
    matrix["backlog"] = coverage_svc.build_backlog(matrix, conflicts)
    return {"data": matrix, "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/runs/{run_id}/pages", dependencies=[Depends(require_api_key)])
async def run_pages(run_id: str, limit: int = Query(default=200, ge=1, le=500),
                    cid: str = Depends(correlation_id)) -> dict:
    """Stored evidence for one run, newest first.

    Thin wrapper: both repositories have carried `get_pages` since the beginning
    and nothing ever called it. That is why the UI could show a quote's
    `@start-end` offsets with no way to open the page those offsets address —
    the proof had no retrieval path, so it was decoration.
    """
    _id_arg("run", run_id)
    if not await asyncio.to_thread(repo().get_run, run_id):
        raise not_found("run", run_id)
    pages = await asyncio.to_thread(repo().get_pages, run_id, limit)
    return {"data": {"run_id": run_id, "pages": pages},
            "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/pages/{page_id}", dependencies=[Depends(require_api_key)])
async def page_detail(page_id: str, cid: str = Depends(correlation_id)) -> dict:
    """One stored page, so a quote's offsets can be resolved against it.

    `markdown` holds the evidence text, and it is what `start`/`end` index into:
    the extractor and the evidence store are built by the same
    `page_evidence_text`, so a verified quote locates in exactly this string.
    Returning anything else here would make the offsets meaningless.

    `raw_html` is dropped from the response. It is the original snapshot —
    routinely megabytes — and quoting a field never needs it; `content_hash`
    plus `get_pages` already covers re-checking that the page did not change.
    """
    _id_arg("page", page_id)
    page = await asyncio.to_thread(repo().get_page, page_id)
    if not page:
        raise not_found("page", page_id)
    page.pop("raw_html", None)
    return {"data": page, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/datasets/{did}/export", dependencies=[Depends(require_api_key)])
async def export_dataset(did: str, body: dict, cid: str = Depends(correlation_id)) -> dict:
    ds = await asyncio.to_thread(repo().get_dataset, did)
    if not ds:
        raise not_found("dataset", did)
    # The caller's chosen columns win over the instance default. The frontend
    # has always sent `fields`; this route ignored it and silently substituted
    # settings.EXPORT_FIELDS, so a user who unticked columns still received all
    # of them — an export that did not match what was on screen.
    req_cols = [str(c).strip() for c in (body.get("fields") or []) if str(c).strip()]
    cols = req_cols or [c.strip() for c in settings.EXPORT_FIELDS.split(",") if c.strip()] or None
    fmt, content, filename = exporter_svc.export_dataset(
        ds.get("schema", ds.get("schema_json", [])), ds.get("records", []),
        body.get("format", "json"),
        cols,
        {"name": ds.get("name", did), "run_id": ds.get("run_id", did),
         "counts": ds.get("counts", {})})
    try:
        export_id = await asyncio.to_thread(repo().save_export, did, fmt, len(content.encode("utf-8")))
        persisted: bool | str = True
    except Exception as e:  # noqa: BLE001 (download must survive persist failure; labeled)
        export_id, persisted = "", f"persist failed: {str(e)[:150]}"
    # Phase-5: each export call is billed work (charge key includes the export
    # row, so repeat exports bill per call while retries stay idempotent).
    credits = 0
    try:
        run_id = ds.get("run_id", did)
        prior = [e for e in await asyncio.to_thread(repo().ledger, run_id) if e.get("stage") == "export"]
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


@app.get("/api/history", dependencies=[Depends(require_api_key)])
async def history(cid: str = Depends(correlation_id)) -> dict:
    return {"data": repo().run_history(), "error": None, "meta": {"correlation_id": cid}}


@app.get("/api/health")
async def health(x_api_key: str | None = Header(default=None)) -> dict:
    """Liveness, plus diagnostics only for a caller who can prove they are
    entitled to them.

    This stays ungated so container health checks and load balancers keep
    working. What it will NOT do is hand configuration to an anonymous caller:
    the persistence adapter, the judge call counters and the throttle state are
    reconnaissance, and an unauthenticated `GET /api/health` is the cheapest
    possible probe for them. So the detail is included only when a valid key is
    presented, and an unauthenticated caller gets a bare liveness answer.
    """
    from app.providers.llm import zen as _zen

    data: dict = {"status": "ok", "version": "0.1.0",
                  "time": datetime.datetime.utcnow().isoformat() + "Z"}

    authorised = bool(settings.API_KEY) and bool(x_api_key) and hmac.compare_digest(
        x_api_key, settings.API_KEY)
    if settings.ALLOW_UNAUTHENTICATED and not settings.API_KEY:
        authorised = True

    if authorised:
        # Which store this process is actually writing to. The adapter used to
        # be inferred from absent keys and fall back silently, so a real
        # database could be bypassed with nothing reporting it.
        data["persistence"] = dict(factory_repo.ACTIVE)
        # Judge throttling, made observable. A run whose fields all came back
        # unverified could be a throttled judge, and that is an operational fact
        # worth seeing without reading logs.
        data["jev"] = dict(_zen.JEV_HEALTH)
    else:
        data["detail"] = "authenticated"
    return {"data": data, "error": None, "meta": {}}


@app.get("/api/models/free", dependencies=[Depends(require_api_key)])
async def free_models(cid: str = Depends(correlation_id)) -> dict:
    """The free-model registry, annotated with live transport reachability.

    Deliberately a registry, not `GET /zen/v1/models`: that endpoint lists 82
    models with null cost, so free-ness cannot be derived from it. This tells
    the UI which transport each model needs and whether it is usable right now.
    """
    from app.providers.llm import opencode as opencode_svc
    from app.services import fanout
    data = fanout.catalogue()
    data["zen"] = {"configured": bool(settings.ZEN_API_KEY),
                   "enabled": bool(settings.ZEN_ENABLED),
                   "base_url": settings.ZEN_BASE_URL}
    data["opencode"] = await opencode_svc.available()
    return {"data": data, "error": None, "meta": {"correlation_id": cid}}


@app.post("/api/intel/ask", dependencies=[Depends(require_api_key)])
async def intel_ask(body: dict, cid: str = Depends(correlation_id)) -> dict:
    """Ask the free tier one question, get every model's answer plus agreement.

    Accepts either a raw `prompt` or a `url`. With a url the page is fetched
    server-side through the same crawler waterfall runs use (robots, rate limit,
    permitted hosts) and reduced to text, so the browser never talks to a target
    site directly.

    One credit, like /api/map.
    """
    from app.providers.llm import zen as zen_svc
    from app.services import fanout, source_router

    prompt = (body.get("prompt", "") or "").strip()
    url = (body.get("url", "") or "").strip()
    if not prompt and not url:
        raise validation("Provide a `prompt` or a `url`.")
    if len(prompt) > 20000:
        raise validation("Prompt too long (20000 chars max).")

    source: dict = {"url": "", "title": "", "chars": 0, "skipped": ""}
    if url:
        try:
            route = source_router.triage_source(url)
        except AppError:
            raise validation(f"Bad intel URL: {url[:120]}")
        try:
            page = await crawler_svc._one(url, route, _fetch_method, asyncio.Semaphore(1))
        except AppError:
            raise
        if page.get("skipped"):
            raise validation(f"Source refused: {page.get('skipped')}")
        from app.services import reducer as reducer_svc
        from app.services import politeness as politeness_svc
        # Same precedence as crawler._one: the rendered rungs (crawl4ai, jina)
        # return `markdown`, only the static rung returns `html`. Reading html
        # alone reported "no readable text" for pages that fetched perfectly.
        # to_thread keeps the parse off the loop serving this request's stream.
        text = page.get("markdown") or await asyncio.to_thread(
            reducer_svc.reduce_html, page.get("html", ""), 20000)
        text = text[:20000]
        thin_at = settings.FETCH_THIN_CHARS
        method = page.get("method", "")
        rendered = method in ("crawl4ai", "impersonate", "jina")
        if not text.strip():
            raise validation("No readable text at that URL.")
        # A JS shell that the renderer could not execute is an infrastructure
        # failure, NOT an analysis result. Measured: startupblink.com returns
        # 483KB of HTML that reduces to 64 chars (the title) because 99% of the
        # document is <script>. When Crawl4AI is busy that thin page was returned
        # silently, and ten models dutifully reported "no companies found" from a
        # bare <title>. In an evidence-first tool that is the worst possible
        # failure: a confident, wrong, unearned negative. Refuse instead.
        if len(text) < thin_at and politeness_svc.looks_js_shell(page.get("html", ""),
                                                                len(text), thin_at):
            raise validation(
                f"Render failed, nothing analysed. That URL is a JavaScript app: "
                f"{len(page.get('html', '')):,}B of HTML reduces to {len(text)} chars "
                f"of text, and the browser renderer "
                f"({'succeeded but was still empty' if rendered else 'did not run'}). "
                f"Any answer here would be invented from the page title alone. "
                f"Retry, or use a static source.")
        source = {"url": page.get("final_url", url),
                  "title": (page.get("title", "") or "")[:300],
                  "chars": len(text), "skipped": "",
                  "method": method, "rendered": rendered,
                  "thin": len(text) < thin_at}
        if not prompt:
            prompt = (f"Read the page below and answer the question. Cite nothing "
                      f"you cannot see in the text.\n\nQUESTION: {body.get('question', '')}\n\n"
                      f"PAGE ({source['title'] or source['url']}):\n{text}")

    requested = body.get("models")
    if isinstance(requested, str):  # tolerate a comma-separated list
        requested = [m.strip() for m in requested.split(",") if m.strip()]
    if requested is not None and (not isinstance(requested, list)
                                  or not all(isinstance(m, str) for m in requested)):
        raise validation("`models` must be a list of model ids.")
    unknown = [m for m in (requested or []) if not zen_svc.is_free(m)]
    if unknown:
        raise validation(f"Unknown model id(s): {', '.join(unknown[:5])}")

    job_id = f"intel-{uuid.uuid4().hex[:8]}"
    entry = metering_svc.Ledger().bill(job_id, "intel", 1)
    try:
        repo().record_charge(job_id, entry)
    except Exception as e:  # noqa: BLE001 (the answer matters more than its receipt)
        log.info("intel ledger persist failed", extra={"data": {"error": str(e)[:150]}})

    out = await fanout.ask_many(prompt, requested or None,
                                system=(body.get("system") or None),
                                json_mode=bool(body.get("json", False)))
    out.update(source=source, job_id=job_id, credits_used=entry["credits"])
    return {"data": out, "error": None, "meta": {"correlation_id": cid}}
