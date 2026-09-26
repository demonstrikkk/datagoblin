"""Deterministic Runner — the ONLY stage owner (docs/07, 35).

Flow: DISCOVERING (supervisor) -> FETCHING (crawler, progressive persist) ->
EXTRACTING -> VALIDATING (+Jev-B) -> NORMALIZE -> DEDUPLICATING -> FINALIZING (atomic).
Partial-safe, cancel-aware, runtime-capped. Emits the 14 SSE event types.
"""
import asyncio
import datetime

from app.core.config import settings
from app.core.constants import RunStage
from app.services import crawler as crawler_svc
from app.services import deduper as deduper_svc
from app.services import discovery as discovery_svc
from app.services import extractor as extractor_svc
from app.services import item_pipeline as pipeline_svc
from app.services import metering as metering_svc
from app.services import normalizer as normalizer_svc
from app.services import provenance as provenance_svc
from app.services import validator as validator_svc


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _emit_stamped(emit: object, run_id: str) -> object:
    async def _stamped(ev: dict) -> None:
        ev = dict(ev)
        ev["run_id"] = run_id
        await _emit(emit, ev)
    return _stamped


async def _emit(emit: object, ev: dict) -> None:
    await emit(ev)


async def _body(run_id: str, plan: dict, ctx: dict, emit: object) -> dict:
    search_fn = ctx["search"]
    llm = ctx.get("llm")
    fetch_fn = ctx.get("fetch")
    store = ctx["store"]
    cancelled = ctx.get("cancelled", lambda: False)

    # Phase-5 metering: project upfront, clamp pages to budget, bill actuals.
    # budget 0/None = unlimited (metering records always, enforces when set).
    projection = metering_svc.project_cost(plan)
    budget = int(plan.get("credit_budget", 0) or 0) or settings.RUN_CREDIT_BUDGET
    meter = metering_svc.Meter(run_id, budget)
    meter.plan(projection["total"])
    record_charge = ctx.get("record_charge")

    async def _bill(stage: str, units: int, seq: int = 0) -> None:
        entry = meter.spend(stage, units, seq)
        if entry is None or record_charge is None:
            return
        try:
            record_charge(entry)
        except Exception as e:  # noqa: BLE001 (ledger best-effort; meter is truth)
            await _emit(emit, {"type": "stage.progress", "run_id": run_id,
                               "stage": RunStage.DISCOVERING,
                               "message": "ledger persist failed "
                                          f"(in-memory totals kept): {str(e)[:120]}",
                               "progress": 5, "timestamp": _now()})

    requested_pages = min(int(plan.get("max_pages", settings.RUN_MAX_PAGES)),
                          settings.RUN_MAX_PAGES)
    allowed_pages = meter.clamp_pages(requested_pages)
    if meter.budget and allowed_pages <= 0:
        await _emit(emit, {"type": "run.failed", "run_id": run_id, "stage": RunStage.FAILED,
                           "message": f"credit budget exhausted before start "
                                      f"(projected {projection['total']} > budget {meter.budget})",
                           "progress": 5, "timestamp": _now()})
        return {"status": "FAILED", "error": "credit budget exhausted"}
    if allowed_pages < requested_pages:
        plan = {**plan, "max_pages": allowed_pages}
        await _emit(emit, {"type": "stage.progress", "run_id": run_id,
                           "stage": RunStage.DISCOVERING,
                           "message": f"budget clamp: max_pages {requested_pages} -> "
                                      f"{allowed_pages} (budget {meter.budget}, "
                                      f"projected {projection['total']})",
                           "progress": 8, "timestamp": _now()})

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.DISCOVERING,
                       "message": "Discovering sources", "progress": 10, "timestamp": _now()})
    sources = await discovery_svc.discover(run_id, plan, search_fn, llm, _emit_stamped(emit, run_id))
    await _bill("discover_query", projection["queries"])
    if cancelled():
        return {"status": "CANCELLED", "records": []}

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.FETCHING,
                       "message": f"Fetching {len(sources)} pages", "progress": 30, "timestamp": _now()})

    async def _persist_source(source: dict) -> None:
        fn = ctx.get("persist_source")
        if fn is not None:
            await fn(source)

    # crawler emits source.fetched itself; wrap emit to stamp run_id
    async def _emit2(ev: dict) -> None:
        await _emit_stamped(emit, run_id)(ev)

    pages, counts = await crawler_svc.fetch_all(sources, fetch_fn, _emit2, _persist_source, plan)
    await _bill("fetch_page", counts["attempted"])
    if cancelled():
        return {"status": "CANCELLED", "records": []}

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.EXTRACTING,
                        "message": f"Extracting ({counts['successful']} ok, {counts['failed']} failed, "
                                   f"{counts.get('skipped', 0)} skipped)",
                        "progress": 55, "timestamp": _now()})
    if meter.exhausted() and pages:
        await _emit(emit, {"type": "stage.progress", "run_id": run_id,
                           "stage": RunStage.EXTRACTING,
                           "message": "credit budget exhausted after fetch; "
                                      "finalizing partial without extraction",
                           "progress": 60, "timestamp": _now()})
        pages = []
    raws: list[dict] = []
    extract_providers: list[str] = []
    for p in pages:
        if cancelled():
            return {"status": "CANCELLED", "records": []}
        try:
            recs, provider = await asyncio.wait_for(
                extractor_svc.extract_page(plan, p, llm),
                timeout=settings.EXTRACT_PAGE_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            recs, provider = [], "timeout"  # stalled page: skip, never stall run
        extract_providers.append(provider)
        for r in recs:
            raws.append(r)
        await _emit(emit, {"type": "record.extracted", "run_id": run_id, "stage": RunStage.EXTRACTING,
                           "message": f"Extracted {len(recs)} from {p['url'][:60]} ({provider})",
                           "progress": 65, "timestamp": _now(),
                           "data": {"url": p["url"], "count": len(recs), "provider": provider}})

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.VALIDATING,
                        "message": f"Validating {len(raws)} records", "progress": 72, "timestamp": _now()})

    async def _validate_stage(raw: dict) -> dict:
        wrapped = await validator_svc.wrap_record(
            plan.get("fields", []), raw, raw.get("source_text", ""),
            raw.get("source_url", ""), raw.get("source_title", ""),
            references=raw.get("references", ""))
        return {"fields": wrapped, "source_url": raw.get("source_url", "")}

    async def _normalize_stage(row: dict) -> dict:
        row["fields"] = normalizer_svc.normalize_record(row.get("fields", {}))
        return row

    # Phase-3 item pipeline: validate -> normalize with bounded Jev concurrency
    # (ITEM_CONCURRENCY), order-preserving kept[], cancel-responsive batches.
    pipe = pipeline_svc.ItemPipeline([("validate", _validate_stage),
                                      ("normalize", _normalize_stage)],
                                     max_concurrency=settings.ITEM_CONCURRENCY)
    kept, dropped, _pstats = await pipe.run(raws, cancelled=cancelled)
    if cancelled():
        return {"status": "CANCELLED", "records": []}
    for d in dropped:  # defensive: current stages mark unverified, never drop
        await _emit(emit, {"type": "record.rejected", "run_id": run_id, "stage": RunStage.VALIDATING,
                           "message": f"Record dropped at {d.get('stage', '?')}: "
                                      f"{d.get('reason', '')[:120]}",
                           "progress": 78, "timestamp": _now(),
                           "data": {"url": (d.get("item", {}) or {}).get("source_url", "")}})
    verified: list[dict] = []
    for row in kept:
        wrapped = row.get("fields", {})
        verified.append({"fields": wrapped})
        bad = [k for k, v in wrapped.items()
               if isinstance(v, dict) and v.get("verification_status") == "unverified"]
        if bad:
            await _emit(emit, {"type": "record.rejected", "run_id": run_id, "stage": RunStage.VALIDATING,
                               "message": f"Record needs review: unverified {', '.join(bad[:5])}",
                               "progress": 78, "timestamp": _now(),
                               "data": {"url": row.get("source_url", ""), "unverified": bad}})
        else:
            await _emit(emit, {"type": "record.verified", "run_id": run_id, "stage": RunStage.VALIDATING,
                               "message": "Record verified", "progress": 78, "timestamp": _now(),
                               "data": {"url": row.get("source_url", "")}})

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.DEDUPLICATING,
                       "message": "Deduplicating", "progress": 85, "timestamp": _now()})
    canonical, merged = deduper_svc.dedupe_records(verified, plan.get("dedupe_keys", []))
    if merged:
        await _emit(emit, {"type": "duplicate.merged", "run_id": run_id, "stage": RunStage.DEDUPLICATING,
                           "message": f"Merged {merged} duplicates", "progress": 88,
                           "timestamp": _now(), "data": {"merged": merged}})

    await _emit(emit, {"type": "stage.progress", "run_id": run_id, "stage": RunStage.DEDUPLICATING,
                       "message": "JUDGE: adjudicating conflicting cells (Jev-C)",
                       "progress": 90, "timestamp": _now()})
    judge_stats = await deduper_svc.adjudicate_conflicts(canonical)
    await _bill("extract_page", sum(1 for p in extract_providers
                                    if p != "none" and not p.startswith("selectors")))
    await _bill("judge_call", judge_stats["judged"])
    if judge_stats["judged"]:
        await _emit(emit, {"type": "stage.progress", "run_id": run_id,
                           "stage": RunStage.DEDUPLICATING,
                           "message": f"JUDGE: {judge_stats['judged']} conflicts "
                                      f"({judge_stats['adopted']} adopted, "
                                      f"{judge_stats['downgraded']} downgraded, "
                                      f"{judge_stats['confirmed']} confirmed)",
                           "progress": 92, "timestamp": _now(), "data": judge_stats})

    summary = provenance_svc.summarize(canonical)
    dataset_id = await store(run_id, plan, canonical,
                             {**counts, "records": len(canonical),
                              "verified": summary["verified"],
                              "needs_review": summary["needs_review"]})
    await _emit(emit, {"type": "run.completed", "run_id": run_id, "stage": RunStage.COMPLETED,
                        "message": f"Done: {len(canonical)} records "
                                   f"({counts['successful']}/{counts['attempted']} sources)",
                        "progress": 100, "timestamp": _now(),
                        "data": {"dataset_id": dataset_id, **counts,
                                 "records": len(canonical),
                                 "verified": summary["verified"],
                                 "needs_review": summary["needs_review"],
                                 "credits_used": meter.spent,
                                 "credits_projected": meter.projected,
                                 "credit_budget": meter.budget}})
    return {"status": "COMPLETED", "records": len(canonical), "dataset_id": dataset_id}


async def execute_run(run_id: str, plan: dict, ctx: dict, emit: object) -> dict:
    try:
        return await asyncio.wait_for(_body(run_id, plan, ctx, emit),
                                      timeout=settings.RUN_MAX_RUNTIME_S)
    except asyncio.TimeoutError:
        await _emit(emit, {"type": "run.failed", "run_id": run_id, "stage": RunStage.FAILED,
                           "message": f"Runtime budget exceeded ({settings.RUN_MAX_RUNTIME_S}s); partial kept",
                           "progress": 50, "timestamp": _now()})
        return {"status": "FAILED", "error": "runtime budget exceeded"}
    except asyncio.CancelledError:
        await _emit(emit, {"type": "run.cancelled", "run_id": run_id, "stage": RunStage.CANCELLED,
                           "message": "Cancelled", "progress": 50, "timestamp": _now()})
        return {"status": "CANCELLED", "records": []}
    except Exception as e:  # noqa: BLE001 (terminal guard; partial preserved upstream)
        await _emit(emit, {"type": "run.failed", "run_id": run_id, "stage": RunStage.FAILED,
                           "message": str(e)[:300], "progress": 50, "timestamp": _now()})
        return {"status": "FAILED", "error": str(e)[:300]}
