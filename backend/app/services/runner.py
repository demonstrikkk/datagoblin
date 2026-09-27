"""Deterministic Runner — the ONLY stage owner (docs/07, 35).

Flow: DISCOVERING (supervisor) -> FETCHING (crawler, progressive persist) ->
EXTRACTING -> VALIDATING (+Jev-B) -> NORMALIZE -> DEDUPLICATING -> FINALIZING (atomic).
Partial-safe, cancel-aware, runtime-capped. Emits the 14 SSE event types.
"""
import asyncio
import datetime
import time

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


class _Budget:
    """Wall-clock budget for one run, checked at stage boundaries.

    Previously a single `asyncio.wait_for` wrapped the whole pipeline, so the
    budget was never visible to any stage: a supervisor loop that burned the
    entire allowance meant FETCH, EXTRACT and VALIDATE never ran at all, and
    the run was cancelled mid-await with nothing to show for it. Stages now see
    the time left and can stop cleanly and keep what they have.
    """

    def __init__(self, total: float) -> None:
        self.total = float(total or 0)
        self.started = time.monotonic()

    def remaining(self) -> float:
        if self.total <= 0:
            return float("inf")
        return max(0.0, self.total - (time.monotonic() - self.started))

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.started


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


async def _body(run_id: str, plan: dict, ctx: dict, emit: object,
                budget: "_Budget | None" = None,
                progress: dict | None = None) -> dict:
    search_fn = ctx["search"]
    llm = ctx.get("llm")
    fetch_fn = ctx.get("fetch")
    store = ctx["store"]
    cancelled = ctx.get("cancelled", lambda: False)
    budget = budget or _Budget(settings.RUN_MAX_RUNTIME_S)
    # Shared with execute_run so a hard timeout can still persist what was
    # produced. Written as each stage completes, never read from the pipeline.
    progress = progress if progress is not None else {}

    # Phase-5 metering: project upfront, clamp pages to budget, bill actuals.
    # 0/None = unlimited (metering records always, enforces when set).
    # Named credit_budget, not udget: udget is the wall-clock allowance
    # and the two were the same local until the collision broke every stage.
    projection = metering_svc.project_cost(plan)
    credit_budget = (int(plan.get("credit_budget", 0) or 0)
                     or settings.RUN_CREDIT_BUDGET)
    meter = metering_svc.Meter(run_id, credit_budget)
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
    async def _discover_emit(ev: dict) -> None:
        """Pass discovery events through, capturing the all-blocked signal.

        "Every candidate was blocked or filtered, and we re-queried twice" is a
        materially different failure from "the web has nothing", and only one of
        them is worth re-running the user's question.
        """
        if ev.get("type") == "source.discovered":
            data = ev.get("data", {}) or {}
            if data.get("all_candidates_blocked_or_filtered"):
                progress["no_yield_reason"] = (
                    f"every candidate was blocked or filtered "
                    f"({len(data.get('blocked_domains') or [])} site(s)) and "
                    f"re-querying did not surface a crawlable source")
            progress["blocked_domains"] = list(data.get("blocked_domains") or [])
        await _emit_stamped(emit, run_id)(ev)

    sources = await discovery_svc.discover(run_id, plan, search_fn, llm, _discover_emit)
    await _bill("discover_query", projection["queries"])
    if cancelled():
        return {"status": "CANCELLED", "records": []}
    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.FETCHING,
                       "message": f"Fetching {len(sources)} pages", "progress": 30, "timestamp": _now()})

    async def _persist_source(source: dict) -> None:
        fn = ctx.get("persist_source")
        if fn is not None:
            await fn(source)

    async def _persist_page(page: dict) -> str:
        """Store the page body; returns the id that evidence will cite.

        Without this the pipeline is crawl -> RAM -> extract -> discard, and
        every quote and offset it emits points into text that no longer exists.
        """
        fn = ctx.get("persist_page")
        return (await fn(page)) if fn is not None else ""

    # crawler emits source.fetched itself; wrap emit to stamp run_id
    async def _emit2(ev: dict) -> None:
        await _emit_stamped(emit, run_id)(ev)

    pages, counts = await crawler_svc.fetch_all(sources, fetch_fn, _emit2, _persist_source,
                                                plan, _persist_page)
    await _bill("fetch_page", counts["attempted"])
    # Published as soon as they exist, so a later timeout can still keep them.
    progress["pages"], progress["counts"] = pages, counts
    if cancelled():
        return {"status": "CANCELLED", "records": []}
    if budget.expired():
        return await _finish_partial(emit, run_id, plan, store, progress, budget,
                                     "runtime budget exhausted after fetching")

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
    partial_reason = ""
    # Pages are extracted concurrently, bounded like the crawler's pool. They
    # were extracted one after another with a spacing sleep between them, so a
    # run's extraction cost was the SUM of every page's LLM latency: measured, a
    # single page could burn the whole 150s page timeout, and 8 pages could not
    # finish inside a 600s budget no matter how much of the work was already
    # done. The budget is now checked as each page lands, not only between them.
    extract_sem = asyncio.Semaphore(
        max(1, int(getattr(settings, "EXTRACT_PAGE_CONCURRENCY", 3) or 3)))
    _ext_conc = max(1, int(getattr(settings, "EXTRACT_PAGE_CONCURRENCY", 3) or 3))

    async def _extract_one(p: dict) -> tuple[list[dict], str]:
        async with extract_sem:
            try:
                return await asyncio.wait_for(
                    extractor_svc.extract_page(plan, p, llm),
                    timeout=settings.EXTRACT_PAGE_TIMEOUT_S)
            except (asyncio.TimeoutError, TimeoutError):
                return [], "timeout"  # stalled page: skip, never stall run

    async def _one(idx: int, p: dict) -> tuple[int, list[dict], str, str]:
        # Stagger by wave, not by index. EXTRACT_PAGE_SPACING_S exists to respect
        # a per-minute free-tier quota, so the spacing is kept - but a flat
        # `spacing * idx` made the last page of a 12-page run wait 110s before it
        # even started, spending the budget on queueing. Waves keep the same
        # calls-per-window while removing the head-of-line delay.
        wave = idx % _ext_conc
        if wave and settings.EXTRACT_PAGE_SPACING_S > 0:
            await asyncio.sleep(settings.EXTRACT_PAGE_SPACING_S * wave)
        recs, provider = await _extract_one(p)
        return idx, recs, provider, str(p.get("url", ""))

    async def _drain() -> None:
        done_n = 0
        for idx, recs, provider, url in await asyncio.gather(
                *(_one(i, p) for i, p in enumerate(pages)), return_exceptions=True):
            if isinstance(recs, BaseException):
                recs, provider, url = [], "error", ""
            done_n += 1
            extract_providers.append(provider)
            for r in recs:
                raws.append(r)
            # Published per page: these are the records a hard timeout would
            # otherwise take with it.
            progress["raws"] = raws
            await _emit(emit, {"type": "record.extracted", "run_id": run_id,
                               "stage": RunStage.EXTRACTING,
                               "message": f"Extracted {len(recs)} from {url[:60]} ({provider})",
                               "progress": 60 + min(9, done_n),
                               "timestamp": _now(),
                               "data": {"url": url, "count": len(recs),
                                        "provider": provider}})
            if budget.expired() and done_n < len(pages):
                await _emit(emit, {"type": "stage.progress", "run_id": run_id,
                                   "stage": RunStage.EXTRACTING,
                                   "message": f"runtime budget exhausted after {done_n} "
                                              f"of {len(pages)} pages",
                                   "progress": 69, "timestamp": _now()})

    if pages:
        if cancelled():
            return {"status": "CANCELLED", "records": []}
        task = asyncio.create_task(_drain())
        # Stop waiting as soon as the budget is gone, but let the in-flight pages
        # finish so their records are not thrown away.
        while not task.done():
            if await asyncio.wait({task}, timeout=2.0) and budget.expired():
                break
            if cancelled():
                task.cancel()
                return {"status": "CANCELLED", "records": []}
        if not task.done():
            task.cancel()
            partial_reason = (f"runtime budget exhausted after "
                              f"{len(extract_providers)} of {len(pages)} pages")
        else:
            await task
            if budget.expired():
                partial_reason = (f"runtime budget exhausted after "
                                  f"{len(extract_providers)} of {len(pages)} pages")
    progress["extract_providers"] = extract_providers
    progress["partial_reason"] = partial_reason

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.VALIDATING,
                        "message": f"Validating {len(raws)} records", "progress": 72, "timestamp": _now()})

    async def _validate_stage(raw: dict) -> dict:
        wrapped = await validator_svc.wrap_record(
            plan.get("fields", []), raw, raw.get("source_text", ""),
            raw.get("source_url", ""), raw.get("source_title", ""),
            references=raw.get("references", ""),
            # The stored page this record came from, so its quote and offsets
            # address evidence that still exists after the run.
            page_id=raw.get("page_id", ""))
        # The gate. Previously nothing ever raised DropItem, so a record whose
        # every field failed verification was still written to the dataset with
        # null values - the pipeline annotated failures instead of rejecting
        # them, and `record.rejected` fired for rows that were kept.
        specs = {f["name"]: f for f in plan.get("fields", []) if f.get("required")}
        missing = [n for n, f in specs.items()
                   if wrapped.get(n, {}).get("value") in (None, "")]
        if missing:
            raise pipeline_svc.DropItem(
                f"required field(s) not evidenced: {', '.join(sorted(missing))}",
                "validate")
        return {"fields": wrapped, "source_url": raw.get("source_url", ""),
                "page_id": raw.get("page_id", "")}

    async def _normalize_stage(row: dict) -> dict:
        row["fields"] = normalizer_svc.normalize_record(row.get("fields", {}))
        return row

    # Phase-3 item pipeline: validate -> normalize with bounded Jev concurrency
    # (ITEM_CONCURRENCY), order-preserving kept[], cancel-responsive batches.
    # The judge budget is per RUN, so concurrent runs in one process cannot
    # starve each other.
    validator_svc.JUDGE_BUDGET = validator_svc.JudgeBudget(
        settings.RUN_MAX_JUDGE_CALLS)
    pipe = pipeline_svc.ItemPipeline([("validate", _validate_stage),
                                      ("normalize", _normalize_stage)],
                                     max_concurrency=settings.ITEM_CONCURRENCY)
    kept, dropped, _pstats = await pipe.run(raws, cancelled=cancelled)
    if cancelled():
        return {"status": "CANCELLED", "records": []}
    for d in dropped:  # a record that failed the gate; counted, not written
        await _emit(emit, {"type": "record.rejected", "run_id": run_id, "stage": RunStage.VALIDATING,
                           "message": f"Record dropped at {d.get('stage', '?')}: "
                                      f"{d.get('reason', '')[:120]}",
                           "progress": 78, "timestamp": _now(),
                           "data": {"url": (d.get("item", {}) or {}).get("source_url", ""),
                                    "reason": d.get("reason", "")[:200]}})
    verified: list[dict] = []
    for row in kept:
        wrapped = row.get("fields", {})
        verified.append({"fields": wrapped})
        # Anything short of verified needs a human: unproven, unjudged or in
        # conflict. These records ARE kept, so they must not be reported as
        # record.rejected - that now means genuinely dropped, and conflating
        # the two made 129 rejections line up with 0 dropped rows.
        bad = [k for k, v in wrapped.items()
               if isinstance(v, dict) and v.get("verification_status") != "verified"]
        if bad:
            await _emit(emit, {"type": "record.needs_review", "run_id": run_id,
                               "stage": RunStage.VALIDATING,
                               "message": f"Record kept for review: {len(bad)} field(s) "
                                          f"not verified ({', '.join(bad[:5])})",
                               "progress": 78, "timestamp": _now(),
                               "data": {"url": row.get("source_url", ""),
                                        "fields": bad[:20]}})
        else:
            await _emit(emit, {"type": "record.verified", "run_id": run_id,
                               "stage": RunStage.VALIDATING,
                               "message": "Record verified", "progress": 78,
                               "timestamp": _now(),
                               "data": {"url": row.get("source_url", "")}})

    await _emit(emit, {"type": "stage.started", "run_id": run_id, "stage": RunStage.DEDUPLICATING,
                       "message": "Deduplicating", "progress": 85, "timestamp": _now()})
    canonical, merged = deduper_svc.dedupe_records(verified, plan.get("dedupe_keys", []))
    # Published before the slow conflict-adjudication step, which is the last
    # thing between here and a stored dataset.
    progress["verified"], progress["canonical"] = verified, canonical
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
    partial = bool(progress.get("partial_reason"))
    reason = progress.get("partial_reason", "")
    no_yield = ""
    if not canonical:
        no_yield = progress.get("no_yield_reason", "") or _explain_zero_yield(
            pages, extract_providers)
    dataset_id = await store(run_id, plan, canonical,
                             {**counts, "records": len(canonical),
                              "partial": partial, "partial_reason": reason,
                              "no_yield_reason": no_yield,
                              **summary})
    if partial:
        headline = f"Partial: {len(canonical)} records kept. {reason}"
    elif canonical:
        headline = (f"Done: {len(canonical)} records "
                    f"({counts['successful']}/{counts['attempted']} sources)")
    else:
        # "0 records" alone reads as a failure of the app. Saying WHY it found
        # nothing is the difference between "this is broken" and "these pages
        # simply do not contain that data".
        headline = (f"Done: 0 records "
                    f"({counts['successful']}/{counts['attempted']} sources) "
                    f"— {no_yield}")
    await _emit(emit, {"type": "run.completed" if not partial else "run.partial",
                       "run_id": run_id, "stage": RunStage.COMPLETED,
                       "message": headline,
                       "progress": 100, "timestamp": _now(),
                       "data": {"dataset_id": dataset_id, **counts,
                                "records": len(canonical),
                                "partial": partial, "partial_reason": reason,
                                "no_yield_reason": no_yield,
                                # Named counts, not "verified"/"needs_review":
                                # those conflated records with fields and
                                # counted unjudged claims as verified.
                                **summary,
                                "credits_used": meter.spent,
                                "credits_projected": meter.projected,
                                "credit_budget": meter.budget}})
    if partial:
        return {"status": "PARTIAL", "records": len(canonical),
                "dataset_id": dataset_id, "partial": True, "error": reason}
    return {"status": "COMPLETED", "records": len(canonical), "dataset_id": dataset_id}


async def _finish_partial(emit: object, run_id: str, plan: dict, store: object,
                          progress: dict, budget: "_Budget", reason: str) -> dict:
    """Persist whatever survived and say plainly that it is partial.

    The timeout path used to emit "partial kept" while `store()` was never
    reached, so every extracted record was discarded and the message described
    the opposite of what happened. This stores the records that exist - never
    unvalidated ones, because publishing unvalidated rows is exactly the thing
    the dataset is supposed to prevent - and reports the real count, including
    zero when nothing survived.
    """
    rows = progress.get("canonical") or progress.get("verified") or []
    counts = progress.get("counts") or {}
    reason = f"{reason} after {budget.elapsed():.0f}s of {budget.total:.0f}s"
    summary = provenance_svc.summarize(rows)
    did = ""
    if rows:
        did = await store(run_id, plan, rows,
                          {**counts, "records": len(rows), "partial": True,
                           "partial_reason": reason, **summary})
    await _emit(emit, {"type": "run.partial", "run_id": run_id, "stage": RunStage.FAILED,
                       "message": (f"{reason}. Kept {len(rows)} validated record(s)"
                                   if rows else
                                   f"{reason}. No records had been validated yet, "
                                   f"so nothing was kept"),
                       "progress": 95, "timestamp": _now(),
                       "data": {"dataset_id": did, "records": len(rows), "partial": True,
                                "partial_reason": reason, **counts, **summary}})
    return {"status": "PARTIAL" if rows else "FAILED",
            "records": len(rows), "dataset_id": did, "partial": True,
            "error": reason}


def _explain_zero_yield(pages: list, providers: list) -> str:
    """Why a run produced no records, in words a user can act on.

    A bare "0 records" is indistinguishable from a broken app. The provider
    tally already knows what happened per page; this turns it into a sentence.
    Measured cases: every page timed out; the LLM was absent; the pages fetched
    fine but were nav-only dashboards with no per-entity rows (the extractor was
    right to find nothing, and the run should say so).
    """
    if not pages:
        return "no page was fetched successfully"
    provs = [str(p or "") for p in providers]
    n = len(provs)
    if not provs:
        return "no page was processed"
    if all(p == "timeout" for p in provs):
        return (f"extraction timed out on all {n} page(s) "
                f"(EXTRACT_PAGE_TIMEOUT_S)")
    if all(p == "none" for p in provs):
        return f"no LLM was available to extract from {n} page(s)"
    if all(p == "error" for p in provs):
        return f"the extraction provider failed on all {n} page(s)"
    thin = sum(1 for p in pages
               if len(p.get("markdown") or p.get("text") or "") < 2000)
    detail = (f"; {thin} of {len(pages)} were near-empty"
              if thin else "")
    return (f"the pages were read but contained no matching records "
            f"({n} page(s) checked, provider {provs[0] or 'none'}){detail}")


async def execute_run(run_id: str, plan: dict, ctx: dict, emit: object) -> dict:
    budget = _Budget(settings.RUN_MAX_RUNTIME_S)
    # Written as each stage completes. The hard timeout below is now a last
    # resort for a stage that ignores its own bound, not the normal way a run
    # ends - stages check `budget.expired()` and stop cleanly instead.
    progress: dict = {}
    store = ctx["store"]
    try:
        return await asyncio.wait_for(
            _body(run_id, plan, ctx, emit, budget, progress),
            # A grace margin over the stage-level budget, so the pipeline gets
            # to notice the budget itself and persist what it has.
            timeout=settings.RUN_MAX_RUNTIME_S + 30)
    except asyncio.TimeoutError:
        # A stage overran its own bound (a hung fetch, an unbounded wait). Keep
        # the work that already completed.
        return await _finish_partial(
            emit, run_id, plan, store, progress, budget,
            "a stage overran the runtime budget")
    except asyncio.CancelledError:
        await _emit(emit, {"type": "run.cancelled", "run_id": run_id, "stage": RunStage.CANCELLED,
                           "message": "Cancelled", "progress": 50, "timestamp": _now()})
        return {"status": "CANCELLED", "records": []}
    except Exception as e:  # noqa: BLE001 (terminal guard; partial preserved upstream)
        await _emit(emit, {"type": "run.failed", "run_id": run_id, "stage": RunStage.FAILED,
                           "message": str(e)[:300], "progress": 50, "timestamp": _now()})
        return {"status": "FAILED", "error": str(e)[:300]}
