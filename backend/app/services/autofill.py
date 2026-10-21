"""Close a run's own coverage gaps before the dataset is handed back.

A run ends with whatever the first crawl extracted. When the pages it fetched
did not carry a field, the column is empty for a reason the user never sees: the
pages that held the answer were never opened.

Measured: a run returned 139 records with `headquarters_city` filled on one of
them. Its three fetched pages contained no headquarters at all, so no amount of
re-reading could help — the field needed a search. The on-demand gap-fill route
(`POST /api/datasets/{id}/gaps/fill`) exists for exactly this, but it only helps
someone who notices the gap and asks. This runs the same pass automatically,
once, against the run's own saved dataset.

It reimplements nothing. It reads the gaps the system already derives
(`coverage` + `gaps`) and drives the same `gapfill.run` the route drives, so the
two cannot disagree about what counts as fillable or how a fill is attempted.

Deliberately bounded and narrow:

* at most `RUN_AUTOFILL_SEARCHES` web searches in total, across all fields;
* at most `RUN_AUTOFILL_FIELDS` fields;
* only `depth_gap` and `schema_gap` fields — a `depth_gap` may reach phase 2
  (a web search), a `schema_gap` only phase 1 (re-reading the stored pages),
  because searching the web for a field no source has ever carried is a
  proposal rather than a default. An `evidence_gap` is a value that exists and
  lacks a verdict, which fetching cannot supply — that is the judge's job;
* a refusal is recorded and skipped, never forced.

It never fails the run. The first crawl already produced a dataset; a failed
second pass is reported and leaves that dataset as it was.
"""
from __future__ import annotations

import datetime
from typing import Any

from app.core.config import settings
from app.core.logging import log
from app.core.constants import RunStage
from app.services import coverage as coverage_svc
from app.services import gapfill as gapfill_svc
from app.services import gaps as gaps_svc

#: Categories whose remedy is reading the value out of a page. `evidence_gap` is
#: absent on purpose: fetching cannot add a verdict.
_FILLABLE = ("depth_gap", "schema_gap")

#: Categories worth spending a web search on. A `schema_gap` is not one: no
#: source has carried the field yet, so a search is a guess at what might, which
#: is a proposal rather than something to run automatically.
_SEARCHABLE = ("depth_gap",)


#: The rule lives in `gaps` because `gapfill` has to apply the same one: the
#: automatic pass deciding a field is worth a search helps nothing if the fill
#: path then refuses it for carrying the `evidence_gap` label.
_materially_missing = gaps_svc.materially_missing


def _fillable(gap: dict) -> bool:
    return str(gap.get("category") or "") in _FILLABLE or _materially_missing(gap)


async def fill_open_gaps(*, store: Any, dataset_id: str, plan: dict, llm: Any,
                         search_fn: Any, fetch_fn: Any, persist_page: Any = None,
                         emit: Any = None, search_budget: int | None = None,
                         max_fields: int | None = None) -> dict:
    """Run one bounded fill pass over a stored dataset's open gaps.

    Returns a report: which fields were attempted, how many searches each spent,
    and how many cells were written. Every number is what actually happened —
    a phase that was refused or skipped says so rather than being omitted.
    """
    budget = int(settings.RUN_AUTOFILL_SEARCHES if search_budget is None
                 else search_budget)
    field_cap = int(settings.RUN_AUTOFILL_FIELDS if max_fields is None
                    else max_fields)
    out: dict = {"fields": [], "searches_run": 0, "cells_written": 0,
                 "budget": budget}
    if budget <= 0 or field_cap <= 0:
        out["skipped"] = "automatic gap-fill is disabled"
        return out
    if not dataset_id:
        out["skipped"] = "the run produced no dataset"
        return out
    if store is None or not gaps_svc.tracked(store):
        out["skipped"] = "this store does not track gap history"
        return out

    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    names = [str(f.get("name")) for f in (plan.get("fields") or [])
             if isinstance(f, dict) and f.get("name")]
    coverage = coverage_svc.field_coverage(records, names)
    # `sync` persists the gaps as well as returning them, so the coverage view
    # and this pass agree about what is missing and what has been tried.
    rows = gaps_svc.sync(store, dataset_id, coverage)
    candidates = [g for g in rows
                  if str(g.get("state") or "open") == "open"
                  and _fillable(g)
                  and int(g.get("attempts") or 0) < gaps_svc.MAX_ATTEMPTS]
    candidates.sort(key=lambda g: -int(g.get("outstanding") or 0))
    candidates = candidates[:field_cap]
    out["considered"] = [str(g.get("field") or "") for g in rows if _fillable(g)]
    if not candidates:
        out["note"] = "no open fillable gap; nothing to search for"
        return out

    remaining = budget
    for cand in candidates:
        field = str(cand.get("field") or "")
        if not field:
            continue
        gap = store.get_gap(dataset_id, field) or cand
        entry = {"field": field, "category": cand.get("category"),
                 "phases": [], "searches_run": 0, "cells_written": 0}
        # A field that is mostly empty goes straight to the search. Phase 1 only
        # re-reads the pages the run just extracted, which is where the absence
        # came from; worse, `record_attempt` resolves the gap as soon as phase 1
        # writes even one cell, so walking the ladder in order spent the search
        # budget on nothing (run `5868f389`: 1 cell written, 0 searches, 105
        # still empty). Phase 1 stays the first step for ordinary depth gaps.
        phase = (gaps_svc.PHASE_SEARCH if _materially_missing(cand)
                 else gaps_svc.next_phase(gap))
        while phase is not None:
            if phase == gaps_svc.PHASE_SEARCH:
                if entry["category"] not in _SEARCHABLE and not _materially_missing(cand):
                    entry["note"] = ("phase 1 found nothing; searching the web "
                                     "for a schema gap is a proposal, not a default")
                    break
                if remaining <= 0:
                    entry["note"] = "search budget spent; phase 2 not attempted"
                    break
            try:
                result = await gapfill_svc.run(
                    store, dataset_id, field, phase=phase, llm=llm,
                    search_fn=search_fn, fetch_fn=fetch_fn,
                    persist_page=persist_page,
                    search_budget=max(1, remaining), apply=True,
                    phrases=plan.get("field_phrases") or None)
            except gapfill_svc.GapFillRefused as exc:
                entry["refused"] = str(exc)[:200]
                break
            except Exception as exc:  # noqa: BLE001 (a failed pass is not a failed run)
                entry["error"] = str(exc)[:200]
                break
            report = result.get("report") or {}
            gap = result.get("gap") or gap
            store.upsert_gap(dataset_id, field, gap)
            used = int(report.get("searches_run") or 0)
            remaining -= used
            entry["phases"].append(phase)
            entry["searches_run"] += used
            entry["cells_written"] += int(report.get("written") or 0)
            if emit is not None:
                await _emit(emit, field, phase, report)
            # Phase 1 only re-reads stored pages and spends no search; a field
            # that survives it is the one worth a web search. Once phase 2 has
            # run, the ladder is exhausted.
            if phase != gaps_svc.PHASE_STORED:
                break
            phase = gaps_svc.next_phase(gap)
        out["fields"].append(entry)
        out["searches_run"] += entry["searches_run"]
        out["cells_written"] += entry["cells_written"]
    return out


async def _emit(emit: Any, field: str, phase: int, report: dict) -> None:
    """Report one phase of the pass, best-effort — a dead stream changes nothing."""
    try:
        await emit({
            "type": "stage.progress",
            "stage": RunStage.FINALIZING,
            "message": (f"Auto gap-fill: phase {phase} on {field} — "
                        f"{int(report.get('written') or 0)} cell(s) written, "
                        f"{int(report.get('searches_run') or 0)} search(es)"),
            "progress": 96,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "data": {"field": field, "phase": phase,
                     "written": int(report.get("written") or 0),
                     "searches_run": int(report.get("searches_run") or 0),
                     "fillable": int(report.get("fillable") or 0)},
        })
    except Exception:  # noqa: BLE001
        log.info("auto gap-fill progress event failed", extra={"data": {"field": field}})
