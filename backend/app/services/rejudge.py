"""Judge the cells that were extracted correctly and then never judged.

992 cells on the live database carry `judgment_unavailable`, which reads as "no
verdict" and is easy to mistake for "no evidence". They have both. Every one of
them has a quote and a `page_id`, and all 38 pages they cite are still stored —
the extraction was right and the check simply never ran.

The reasons it never ran are two, and neither is a broken judge:

* **The per-run budget.** `RUN_MAX_JUDGE_CALLS` defaults to 400 and a live run
  extracted 728 records, so the cap ran out mid-dataset. The Y Combinator dataset
  has 726 cells and 556 unjudged — more than the budget could ever have covered.
* **The judge was unreachable** for part of some runs. Indian stocks has 124 cells,
  nowhere near the cap, and 80 unjudged.

Neither is fixed by a different provider. Both are fixed by judging the cells
that already have their evidence on disk, which costs no crawl and no fetch.

## Most of them cost nothing at all

`jev.deterministic_verdict` runs before any provider call and settles two cases by
arithmetic: a quote that is not on the page cannot support anything, and a value
literally inside its own quote needs no interpretation. Measured over the 992:

| outcome | cells | cost |
|---|---|---|
| settled deterministically | **733** | free, no provider call |
| genuinely needs a judgement | **259** | one call each |

733 of them are dominated by identity fields — `company_name` (198), `yc_slug`
(50), `batch_season` (50) — which is exactly where "is the value literally in the
quote" is the right question. So the real cost of closing this gap is 259 calls,
not 992, and a budget that refused the work at 400 could not have refused 259.

## This path upgrades verdicts and never removes data

The contract is deliberately one-directional. `verify_field` returns `None` for a
NOT_SUPPORTED judgement, which is correct *at extraction time* because there is
nothing to keep and the cell was never in the dataset. Re-judging is a different
situation: the value is already stored, with a real quote and a real page behind
it, and it was accepted once already.

So a judge disagreeing on a second pass does not delete anything here. It marks
the cell `unverified` — present, not proven — and records the disagreement, which
is the honest description of "we extracted this, and a later check says the quote
does not support it". Deleting stored data on the strength of a second opinion
would make this path strictly more destructive than the problem it fixes.

`judgment_unavailable` also stays `judgment_unavailable` when the judge is
uncertain, throttled, or absent. Upgrading a cell to "we could not tell" from
"we could not tell" is not progress, and reporting it as progress is how a
dataset comes to look checked when it is not.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services import gaps as gaps_svc
from app.providers.decision import jev
from app.services.validator import JudgeBudget

#: Judge calls allowed in one pass. 259 is the entire measured hard set for this
#: dataset, so this ceiling is not reached by the work that exists; it exists so a
#: re-judge on a much larger workspace is bounded rather than open-ended.
JUDGE_CALLS_PER_PASS = 600

#: Concurrency. Provider calls are I/O-bound and this provider has been measured
#: at a very stable ~1.44s median, so the pass is latency-bound rather than
#: rate-bound; 4 keeps it short without being the kind of burst that provokes a
#: 429.
CONCURRENCY = 4


class RejudgeRefused(Exception):
    """The request cannot be served safely. Reported, never partially applied."""


def _cells_needing_a_verdict(records: list[dict], field: str | None = None) -> list[dict]:
    """Every stored cell whose status says a check never ran.

    Filtered on `judgment_unavailable` alone. A `verified`, `unverified`,
    `conflicting` or `rate_limited` cell already has a verdict and is left alone:
    re-deciding settled cells on a later pass would make the dataset's history
    unreproducible, and nothing here gains from it.
    """
    out: list[dict] = []
    for rec in records:
        rid = str(rec.get("record_id") or "")
        for name, cell in (rec.get("fields") or {}).items():
            if field and name != field:
                continue
            if not isinstance(cell, dict):
                continue
            if str(cell.get("verification_status") or "") != "judgment_unavailable":
                continue
            src = cell.get("source") or {}
            if not src.get("quote") or not src.get("page_id"):
                # No evidence to judge against. Left untouched rather than
                # reported as a failure — this is an extraction gap, and the
                # coverage view already classifies it as one.
                continue
            out.append({
                "record_id": rid, "field": name, "cell": cell,
                "value": cell.get("value"), "quote": src.get("quote", ""),
                "page_id": str(src.get("page_id")),
            })
    return out


def _pages_for(store, cells: list[dict]) -> dict[str, str]:
    """The stored evidence text for every page these cells cite.

    One read per *distinct page*, not per cell. The 992 cells cite 38 pages, so
    the naive shape would be 992 reads of which 954 were redundant — the same
    mistake `_candidate_pages` documents, where re-fetching identical evidence
    cost seconds and paid nothing.
    """
    needed = sorted({c["page_id"] for c in cells if c.get("page_id")})
    if not needed:
        return {}
    by_ids = getattr(store, "get_pages_by_ids", None)
    rows: list[dict] = []
    if callable(by_ids):
        rows = by_ids(needed) or []
    else:
        for pid in needed:
            page = store.get_page(pid)
            if page:
                rows.append(page)
    out: dict[str, str] = {}
    for row in rows:
        pid = str(row.get("id") or "")
        # `markdown` is the evidence text the store wrote, which is exactly what
        # the extractor read, so a quote re-locates against the same string.
        text = row.get("markdown") or ""
        if not text:
            text = row.get("raw_html") or row.get("html") or ""
        if pid:
            out[pid] = text
    return out


def _upgraded(cell: dict, judgment: str, confidence: float) -> dict:
    """The cell after a judgement. The value is never touched.

    Three outcomes, and the third is the important one:

    * SUPPORTED -> `verified`. Strictly more information than before.
    * NOT_SUPPORTED -> `unverified` plus a recorded disagreement. The value stays,
      because it was already stored with a quote and deleting it on a second
      opinion would make this path more destructive than the gap it closes.
    * anything else -> unchanged. `judgment_unavailable` stays
      `judgment_unavailable`; "we could not tell" is not an improvement on "we
      could not tell".
    """
    if judgment == "SUPPORTED":
        return {**cell, "verification_status": "verified",
                "judgment_confidence": confidence}
    if judgment == "NOT_SUPPORTED":
        return {**cell, "verification_status": "unverified",
                "rejudged": "not_supported",
                "judgment_confidence": confidence}
    return cell


async def run(store, dataset_id: str, *, apply: bool = False,
              field: str | None = None,
              judge_budget: int = JUDGE_CALLS_PER_PASS) -> dict:
    """Judge the cells of one dataset that carry no verdict. Writes nothing unless
    `apply`.

    `apply=False` performs every judgement for real and withholds only the write,
    so the numbers are measured rather than estimated — and a pass that is not
    applied must not mark anything, which is why `unresolved` stays unresolved
    rather than being optimistically promoted.
    """
    if judge_budget < 0:
        raise RejudgeRefused("judge_budget must not be negative")

    row = getattr(store, "get_dataset_row", lambda _id: None)(dataset_id)
    if not row:
        raise RejudgeRefused("unknown dataset")
    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    if not records:
        raise RejudgeRefused("this dataset has no records to judge")

    pending = _cells_needing_a_verdict(records, field)
    if not pending:
        return _empty(dataset_id, apply, field,
                      "every cell either carries a verdict or has no evidence to "
                      "judge against; nothing to do")

    pages = _pages_for(store, pending)
    judgeable = [c for c in pending if pages.get(c["page_id"])]
    if not judgeable:
        # Reported with the real counts rather than zeros: "no evidence" and
        # "nothing to do" are different answers, and a caller told there was
        # nothing to do would not go looking for the pages.
        return _empty(dataset_id, apply, field,
                      f"none of the {len(pending)} unjudged cell(s) has retrievable "
                      f"evidence; the pages they cite are not in the store, which "
                      f"is a different problem from an unjudged verdict",
                      pending=len(pending))

    budget = JudgeBudget(judge_budget)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(item: dict) -> tuple[dict, str, float, str]:
        async with sem:
            text = pages.get(item["page_id"], "")
            # The deterministic verdict runs first and is free, so a budget unit
            # is only spent on a judgement that genuinely needs one. Same order
            # as `verify_field`, and for the same reason: a cap is supposed to
            # bound the expensive thing.
            quick = jev.deterministic_verdict(str(item["value"]), item["quote"], text)
            if quick is not None:
                return item, quick["judgment"], float(quick.get("confidence") or 0.0), "deterministic"
            if budget.exhausted or not budget.spend():
                return item, "", 0.0, "budget"
            try:
                verdict = await jev.evidence_verification(
                    str(item["value"]), item["quote"], text)
            except Exception:  # noqa: BLE001 (one bad call must not stop the pass)
                return item, "", 0.0, "error"
            return (item, str(verdict.get("judgment") or ""),
                    float(verdict.get("confidence") or 0.0), "jev")

    results = await asyncio.gather(*(_one(c) for c in judgeable))

    free = judged = unresolved = 0
    upgrades: list[dict] = []
    disagreements: list[dict] = []
    for item, judgment, confidence, how in results:
        if not judgment:
            unresolved += 1
            continue
        if how == "deterministic":
            free += 1
        else:
            judged += 1
        after = _upgraded(item["cell"], judgment, confidence)
        if after is item["cell"] or after.get("verification_status") == "judgment_unavailable":
            unresolved += 1
            continue
        row_out = {
            "record_id": item["record_id"], "field": item["field"],
            "cell": after, "url": (item["cell"].get("source") or {}).get("url", ""),
            "page_id": item["page_id"], "how": how,
        }
        if after.get("verification_status") == "verified":
            upgrades.append(row_out)
        else:
            disagreements.append(row_out)

    written = 0
    if apply:
        for item in upgrades + disagreements:
            if store.update_record_cell(dataset_id, item["record_id"],
                                        item["field"], item["cell"]):
                written += 1

    return {
        "dataset_id": dataset_id,
        "applied": bool(apply),
        "field": field or "",
        "unjudged_found": len(pending),
        "evidence_available": len(judgeable),
        "evidence_missing": len(pending) - len(judgeable),
        "settled_free": free,
        "judge_calls": judged,
        "judge_calls_allowed": judge_budget,
        "verified_now": len(upgrades),
        "disagreements": len(disagreements),
        "still_unjudged": unresolved,
        "written": written,
        "disagreement_examples": [
            {"field": d["field"], "value": d["cell"].get("value"),
             "quote": (d["cell"].get("source") or {}).get("quote", "")[:160],
             "url": d["url"]}
            for d in disagreements[:5]
        ],
        "note": ("every judgement ran for real; nothing was written" if not apply
                 else f"{written} verdict(s) written; no value was changed or removed"),
    }


def _empty(dataset_id: str, apply: bool, field: str | None, why: str,
           pending: int = 0) -> dict:
    """A pass that judged nothing. `pending` keeps the distinction between
    "there was nothing outstanding" and "everything outstanding is unjudgeable"."""
    return {
        "dataset_id": dataset_id, "applied": bool(apply), "field": field or "",
        "unjudged_found": int(pending), "evidence_available": 0,
        "evidence_missing": int(pending), "settled_free": 0, "judge_calls": 0,
        "judge_calls_allowed": 0, "verified_now": 0, "disagreements": 0,
        "still_unjudged": int(pending), "written": 0,
        "disagreement_examples": [], "note": why,
    }


def is_rejudgeable(gap: dict) -> tuple[bool, str]:
    """Whether a gap is one this path can act on, and why not if it cannot.

    Shares the vocabulary with `gapfill` so the two features cannot disagree about
    what an `evidence_gap` is. An `evidence_gap` is exactly the population this
    path exists for — which is why `gapfill` refuses to search for one, and why
    this is a separate action rather than a phase.
    """
    if str(gap.get("category")) != "evidence_gap":
        return False, ("only an evidence gap is a re-judge candidate; this one is "
                       "missing values rather than unproven ones, so the remedy is "
                       "fetching, not judging")
    allowed, why = gaps_svc.can_attempt(gap, phase=gaps_svc.PHASE_STORED)
    if not allowed and str(gap.get("state")) in ("resolved", "exhausted", "refused"):
        return False, why
    return True, ""
