"""Fill fields a dataset never extracted, without crawling anything again.

The Coverage view could report `batch_type: 1.4% — never extracted` and the only
remedy on offer was a whole new run: re-discover, re-fetch, re-reduce, re-extract
every field, re-judge every cell. Every field that already carried a verified
quote was paid for again to obtain one field that was missing.

This closes that gap. It reads pages the run already stored, asks for **only**
the absent fields, and fills only cells that are currently empty. The cost is
LLM calls, not requests.

Three rules make it safe to run against real data:

1. **Only fields no record has.** A field that some records carry is a coverage
   gap; a field no record carries is a schema problem the sources may simply not
   answer. Mixing the two would quietly widen what backfill rewrites.
2. **Exact identity only.** A new extraction is matched to an existing record
   by `deduper._sig` over the plan's `dedupe_keys` and nothing else. The
   deduper's fuzzy tier exists to *merge rows*; using it here would attach a
   value to a record it does not belong to, and because that value then carries
   a real quote and a real verdict, nothing downstream would look wrong. An
   unmatched extraction is reported, never guessed.
3. **A backfilled value is a verified value or it is not kept.** New cells go
   through the same `wrap_record` the run uses, with a fresh judge budget. A
   cell written here is indistinguishable from one written at run time, which is
   the only way the verification contract survives this feature existing.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services import coverage as coverage_svc
from app.services import extractor as extractor_svc
from app.services import validator as validator_svc
from app.services.deduper import _sig
from app.services.reducer import page_evidence_text

#: Beyond this the proposer stops looking. A proposal has to be reviewable, and
#: a hundred candidate pages is not a decision anyone can make.
MAX_CANDIDATE_PAGES = 40

#: Judge calls allowed per backfill, per record. The run's own budget is spent;
#: this is a separate, deliberately small allowance so a backfill cannot quietly
#: become the most expensive thing in the system.
JUDGE_CALLS_PER_RECORD = 6


class BackfillRefused(Exception):
    """The request cannot be served safely. Reported, never partially applied."""


def plan_for_dataset(store, dataset_id: str) -> dict:
    """The full plan behind a dataset, recovered through its run.

    `dedupe_keys` is the whole basis of identity matching and it is not stored
    on the dataset — only the fields are. It is recovered the same way the run
    did, by walking dataset -> run -> workflow, so a backfill cannot invent a
    key set that does not match how the rows were actually deduplicated.
    """
    ds = store.get_dataset(dataset_id)
    if not ds:
        raise BackfillRefused("unknown dataset")
    run_id = ds.get("run_id", "")
    if not run_id:
        raise BackfillRefused("dataset has no run, so its plan cannot be recovered")
    run = store.get_run(run_id)
    if not run:
        raise BackfillRefused(f"run {run_id} is gone, so the plan cannot be recovered")
    plan = store.get_plan(run.get("workflow_id", ""))
    if not plan:
        raise BackfillRefused("the plan behind this run is not stored")
    return plan


def _plain(fields: dict) -> dict:
    """{name: cell} -> {name: value}. Cells and bare scalars both occur."""
    return {k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in (fields or {}).items()}


def absent_fields(coverage: dict, requested: list[str] | None = None) -> list[str]:
    """Fields with no value on any record — never merely partial ones."""
    names = [f["field"] for f in (coverage.get("fields") or []) if f.get("present") == 0]
    if requested:
        wanted = {str(n) for n in requested}
        names = [n for n in names if n in wanted]
    return names


def _candidate_pages(store, run_id: str, limit: int) -> list[dict]:
    """Stored pages worth re-reading, biggest evidence first.

    Full page rows, because extraction needs the DOM for the deterministic
    selector path and the markdown for the evidence text. `get_pages` omits
    raw_html on purpose (it is a listing endpoint), so each candidate is read
    in full by id.
    """
    listing = store.get_pages(run_id, limit=MAX_CANDIDATE_PAGES) or []
    pages = []
    for row in listing:
        full = store.get_page(row["id"])
        if full and (full.get("markdown") or full.get("raw_html")):
            pages.append(full)
        if len(pages) >= limit:
            break
    return pages


def propose(store, dataset_id: str, fields: list[str] | None = None,
            limit_pages: int = 8) -> dict:
    """What a backfill would do, and what it would cost. Writes nothing.

    The cost is the point. Without it, "fill the missing fields" is an
    unbounded-sounding instruction and the only way to find out what it costs is
    to do it.
    """
    plan = plan_for_dataset(store, dataset_id)
    ds = store.get_dataset(dataset_id) or {}
    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    matrix = coverage_svc.field_coverage(
        records, ds.get("schema", ds.get("schema_json", [])) or plan.get("fields", []))
    targets = absent_fields(matrix, fields)
    if not targets:
        return {"dataset_id": dataset_id, "fields": [], "backfillable": False,
                "reason": "every declared field already has a value on at least one record",
                "pages": 0, "estimated_judge_calls": 0}

    keys = [str(k) for k in (plan.get("dedupe_keys") or [])]
    # The dedupe keys have to be extracted too. Without them a new record has no
    # identity to be matched on, and the whole operation would find nothing it
    # could safely attach to anything.
    extract_fields = sorted(set(targets) | set(keys))
    pages = _candidate_pages(store, ds.get("run_id", ""), max(1, int(limit_pages)))
    if not pages:
        return {"dataset_id": dataset_id, "fields": targets, "backfillable": False,
                "reason": "this run stored no readable pages, so there is nothing to re-read",
                "pages": 0, "estimated_judge_calls": 0}

    matchable = 0
    for rec in records:
        if _sig(_plain(rec.get("fields", {})), keys):
            matchable += 1

    return {
        "dataset_id": dataset_id,
        "fields": targets,
        "backfillable": bool(pages) and bool(matchable) and bool(keys),
        "reason": (
            "" if (keys and matchable and pages) else
            "this plan declares no dedupe_keys, so a new extraction could not be "
            "matched to a record safely — no values would be written"
            if not keys else
            f"{matchable} of {len(records)} records carry an identity signature"
            if not matchable else
            "no stored pages to re-read"
        ),
        "dedupe_keys": keys,
        "extract_fields": extract_fields,
        "records": len(records),
        "matchable_records": matchable,
        "pages": len(pages),
        "page_urls": [p.get("url", "") for p in pages],
        # One extraction call per page, and a judge call per field per record
        # that actually matches. Both are shown so the number is not a surprise.
        "estimated_extractions": len(pages),
        "estimated_judge_calls": len(pages) * JUDGE_CALLS_PER_RECORD,
        "reuses_stored_pages": True,
    }


async def run(store, dataset_id: str, fields: list[str] | None = None, *,
              llm: Any, limit_pages: int = 8, apply: bool = False) -> dict:
    """Re-extract the absent fields from stored pages and report what would fill.

    `apply=False` is a dry run that still does the real extraction and the real
    verification, and stops before writing. It exists so the proposal's numbers
    can be checked against reality rather than trusted — the difference between
    "8 pages, about 48 judge calls" and what actually happens is exactly the
    kind of thing that should not be discovered by a write.
    """
    plan = plan_for_dataset(store, dataset_id)
    ds = store.get_dataset(dataset_id) or {}
    run_id = ds.get("run_id", "")
    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    matrix = coverage_svc.field_coverage(
        records, ds.get("schema", ds.get("schema_json", [])) or plan.get("fields", []))
    targets = absent_fields(matrix, fields)
    if not targets:
        # Same keys as the full return below. An early exit that renames
        # `fillable` to `filled` is a shape the caller has to special-case, and
        # the one place it matters is the "nothing to do" case — exactly the
        # case a caller checks first.
        return {"dataset_id": dataset_id, "applied": bool(apply),
                "pages_read": 0, "records_matched": 0,
                "fillable": 0, "written": 0, "filled_fields": {},
                "skipped_existing": 0, "unmatched": 0, "unmatched_examples": [],
                "judge_calls_used": 0, "reuses_stored_pages": True,
                "fields": [],
                "reason": "no field is entirely absent, so there is nothing to backfill"}

    keys = [str(k) for k in (plan.get("dedupe_keys") or [])]
    if not keys:
        raise BackfillRefused(
            "this plan declares no dedupe_keys, so a fresh extraction could not be "
            "matched to an existing record. Writing anyway would risk attaching a "
            "value to the wrong row, so nothing was written.")

    spec_by_name = {str(f.get("name")): f for f in (plan.get("fields") or [])
                    if isinstance(f, dict) and f.get("name")}
    extract_names = sorted(set(targets) | set(keys))
    extract_specs = [spec_by_name[n] for n in extract_names if n in spec_by_name]
    if not extract_specs:
        raise BackfillRefused("the plan's schema does not describe the requested fields")

    pages = _candidate_pages(store, run_id, max(1, int(limit_pages)))
    if not pages:
        raise BackfillRefused("this run stored no readable pages to re-read")

    reduced_plan = {**plan, "fields": extract_specs}

    # Signature -> record. Only records that already have an identity can be
    # filled at all; a record with no signature carries no evidence of what it
    # is, and matching it to anything would be a guess.
    by_sig: dict[str, dict] = {}
    for rec in records:
        sig = _sig(_plain(rec.get("fields", {})), keys)
        if sig:
            by_sig.setdefault(sig, rec)

    filled: list[dict] = []
    skipped_existing = 0
    unmatched: list[dict] = []
    matched_records: set[str] = set()

    sem = asyncio.Semaphore(3)

    async def _one_page(page: dict) -> list[dict]:
        async with sem:
            # The same reduction the run used. Rebuilding the evidence text any
            # other way would produce offsets that do not address the stored
            # markdown, and every quote in a backfilled cell would become
            # unresolvable — a silent break of the one guarantee this product
            # makes.
            working = {**page, "markdown": page.get("markdown", "")}
            try:
                source_text = await asyncio.to_thread(page_evidence_text, working)
            except Exception:  # noqa: BLE001
                return []
            working["_evidence_text"] = source_text
            try:
                recs, _provider = await extractor_svc.extract_page(reduced_plan, working, llm)
            except Exception:  # noqa: BLE001
                return []
            return recs or []

    batches = await asyncio.gather(*(_one_page(p) for p in pages))

    for recs in batches:
        for raw in recs:
            if not raw.get("fields"):
                continue
            sig = _sig(_plain(raw.get("fields", {})), keys)
            record = by_sig.get(sig) if sig else None
            if record is None:
                unmatched.append({"fields": _plain(raw.get("fields", {})),
                                  "url": raw.get("source_url", ""),
                                  "reason": "no existing record carries this identity"})
                continue
            record_id = str(record.get("record_id") or "")
            if record_id in matched_records:
                # Two pages legitimately describe the same entity. The first
                # fill wins; a second would be a duplicate the deduper already
                # decided about, and this path does not re-open that decision.
                continue
            matched_records.add(record_id)

            # The judge budget is per record, not per run, so one record with an
            # unusual number of fields cannot spend the whole allowance and
            # leave the rest unverified.
            budget = validator_svc.JudgeBudget(JUDGE_CALLS_PER_RECORD)
            cells = await validator_svc.wrap_record(
                extract_specs, raw, raw.get("source_text", "") or raw.get("_evidence_text", ""),
                raw.get("source_url", ""), raw.get("source_title", ""),
                references=raw.get("references", ""),
                page_id=raw.get("page_id", ""), budget=budget)

            existing = record.get("fields", {})
            for name, cell in cells.items():
                if name not in targets:
                    continue
                current = existing.get(name)
                has_value = isinstance(current, dict) and current.get("value") not in (None, "")
                if has_value:
                    # Never overwrite. A cell that already carries a value and a
                    # verdict was earned; replacing it because a second page
                    # said something else is the deduper's job, not this one's.
                    skipped_existing += 1
                    continue
                if cell.get("value") in (None, ""):
                    continue
                filled.append({
                    "record_id": record_id,
                    "field": name,
                    "cell": cell,
                    "page_id": raw.get("page_id", ""),
                    "url": raw.get("source_url", ""),
                })

    written = 0
    if apply:
        for item in filled:
            if store.update_record_cell(dataset_id, item["record_id"],
                                        item["field"], item["cell"]):
                written += 1

    per_field: dict[str, int] = {}
    for item in filled:
        per_field[item["field"]] = per_field.get(item["field"], 0) + 1

    return {
        "dataset_id": dataset_id,
        "applied": bool(apply),
        "fields": targets,
        "pages_read": len(pages),
        "records_matched": len(matched_records),
        "fillable": len(filled),
        "written": written,
        "filled_fields": per_field,
        "skipped_existing": skipped_existing,
        "unmatched": len(unmatched),
        "unmatched_examples": unmatched[:5],
        "judge_calls_used": JUDGE_CALLS_PER_RECORD * len(matched_records),
        "reuses_stored_pages": True,
        "note": ("extraction and verification ran for real; nothing was written"
                 if not apply else "values written, each with its own quote and verdict"),
    }
