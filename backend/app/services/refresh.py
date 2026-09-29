"""Re-read the sources a dataset already used, and re-verify what they said.

Backfill answers "this cell is empty, can it be filled from a page we already
have". It cannot answer "this value was wrong, or the page has changed since",
because both of those are about a cell that *holds* something — and every rule
in `backfill` exists to refuse to touch a cell that holds something.

So refresh is a separate operation with a deliberately different write rule, and
the difference is the point:

* a source is re-fetched with `force` semantics, never reused, because reusing
  the stored copy is what made the value stale in the first place;
* a new value is only ever written when it carries its own verified quote, so
  "refreshed" cannot mean "replaced by something unproven";
* a value that is still the same is recorded as unchanged, not rewritten, so a
  refresh does not churn every cell in the dataset;
* a value that differs is written **with the old value preserved as a rival**,
  so the conflict stays visible and resolvable instead of being silently
  decided by whichever page happened to be fetched last.

That last rule is why this is not backfill with a flag. The verification model
promises that a disputed value can be seen and judged; overwriting in place
would quietly break that promise for the one case where the human is most likely
to want to look.

Nothing here decides anything. Every write goes through `validator.wrap_record`
and lands in the same cell shape the run produces, and a refresh that finds
nothing different returns a report saying so rather than a count that could be
read as progress.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services import validator as validator_svc
from app.services import yieldmap as yieldmap_svc
from app.services.deduper import _sig
from app.services.reducer import page_evidence_text

#: Judge calls allowed per record for a refresh. Lower than backfill's: a
#: refresh is re-checking a smaller, named set of cells, and a runaway budget
#: here would spend more than the run that produced the dataset.
JUDGE_CALLS_PER_RECORD = 6

#: Sources re-fetched per request. A refresh over a 500-source dataset is not a
#: repair, it is a re-run wearing a different name.
MAX_SOURCES = 10


class RefreshRefused(Exception):
    """The request cannot be served safely. Reported, never partially applied."""


def _plain(fields: dict) -> dict:
    return {k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in (fields or {}).items()}


def _cell_value(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get("value")
    return cell


def pick_sources(store, dataset_id: str, wanted: list[str] | None = None,
                 limit: int = MAX_SOURCES) -> list[dict]:
    """Which sources to re-read.

    Explicitly named hosts win, and the naming is matched on host rather than
    full URL so "re-read techcrunch" selects that site without the user having to
    know which of its forty URLs the dataset used. Unnamed sources are ordered by
    recorded yield, because if the user is spending refresh budget on five
    fetches they should be the five that have historically produced verified
    cells — not the five that happen to be alphabetically first.
    """
    block = store.get_sources(dataset_id) or {}
    sources = [s for s in (block.get("sources") or [])
               if str(s.get("status") or "") in ("ok", "reused")]
    if not sources:
        return []

    if wanted:
        wanted_l = {str(w).strip().lower() for w in wanted if str(w).strip()}
        if not wanted_l:
            return []
        picked = []
        for src in sources:
            host = yieldmap_svc.host_of(src.get("url"))
            url = str(src.get("url") or "").lower()
            if host in wanted_l or url in wanted_l or any(
                    host.endswith("." + w) for w in wanted_l):
                picked.append(src)
        if not picked:
            return []
        return picked[:limit]

    try:
        ym = yieldmap_svc.build(store, dataset_id)
        rank = {h: i for i, h in enumerate(ym.get("ranking") or [])}
    except Exception:  # noqa: BLE001
        rank = {}
    sources.sort(key=lambda s: (rank.get(yieldmap_svc.host_of(s.get("url")), 10**6),
                                s.get("url", "")))
    return sources[:limit]


def _nothing_to_read(store, dataset_id: str, sources: list[str] | None) -> dict:
    """Why a refresh would do nothing, naming what it looked for and what exists.

    "No source matched" is a true sentence and a useless one when the dataset has
    forty sources — the reader has no way to learn which host it should have said.
    So the refusal lists the hosts this dataset actually used.
    """
    out = {"dataset_id": dataset_id, "refreshable": False, "sources": 0,
           "named": [str(s) for s in (sources or [])], "hosts": [], "urls": []}
    if not sources:
        out["reason"] = "this dataset recorded no usable source to re-read"
        return out
    used = sorted({yieldmap_svc.host_of(s.get("url"))
                   for s in (store.get_sources(dataset_id) or {}).get("sources") or []
                   if yieldmap_svc.host_of(s.get("url"))})
    out["hosts"] = used
    out["reason"] = (
        f"nothing named {', '.join(str(s) for s in sources)} is one of this "
        f"dataset's sources. It used: {', '.join(used) or 'none'}"
        if used else
        f"nothing named {', '.join(str(s) for s in sources)} is a source of this "
        f"dataset, which recorded none")
    return out


def propose(store, dataset_id: str, sources: list[str] | None = None,
           limit: int = MAX_SOURCES) -> dict:
    """What a refresh would re-read and what it would cost. Writes nothing."""
    chosen = pick_sources(store, dataset_id, sources, limit)
    if not chosen:
        return _nothing_to_read(store, dataset_id, sources)

    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    # Only cells that came from a source being refreshed can change, so the
    # re-verified set is named rather than the whole dataset. Quoting this
    # number is the difference between "re-read 8 pages" and "re-judged 4,100
    # cells".
    hosts = {yieldmap_svc.host_of(s.get("url")) for s in chosen}
    urls = {str(s.get("url")) for s in chosen}
    affected = 0
    for rec in records:
        for _name, cell in (rec.get("fields") or {}).items():
            if not isinstance(cell, dict) or _cell_value(cell) in (None, ""):
                continue
            src = cell.get("source") or {}
            if (src.get("url") in urls
                    or yieldmap_svc.host_of(src.get("url")) in hosts):
                affected += 1

    return {
        "dataset_id": dataset_id,
        "refreshable": bool(chosen),
        "named": [str(s) for s in (sources or [])],
        "sources": len(chosen),
        "urls": [s.get("url", "") for s in chosen],
        "hosts": sorted(h for h in hosts if h),
        "cells_reverified": affected,
        "estimated_extractions": len(chosen),
        "estimated_judge_calls": len(chosen) * JUDGE_CALLS_PER_RECORD,
        "refetches": True,
        "writes_only_verified": True,
        "preserves_rivals": True,
    }


async def run(store, dataset_id: str, sources: list[str] | None = None, *,
              llm: Any, limit: int = MAX_SOURCES, apply: bool = False,
              fetch=None) -> dict:
    """Re-read the chosen sources and re-verify the values they supplied.

    `fetch` is the crawler's single-page callable, injected rather than imported
    so the fetch waterfall, its politeness rules and its per-run budget stay in
    one place, and so this path is testable without a network. It is called once
    per URL and is expected to go straight to the network: reuse is consulted
    inside `crawler.fetch_all`, and refresh deliberately does not go through the
    queue, so the stored copy is never a candidate. Reusing it would re-read the
    snapshot that produced the stale value and report it as fresh.

    `apply=False` re-fetches, re-extracts and re-verifies for real and withholds
    only the writing, for the same reason backfill has that mode: the proposal's
    numbers should be checkable against what happens, not believed in advance.
    """
    chosen = pick_sources(store, dataset_id, sources, limit)
    if not chosen:
        raise RefreshRefused(
            "no named source matches a URL or host this dataset used, so there is "
            "nothing to re-read")

    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    try:
        plan = store.get_plan((store.get_dataset_row(dataset_id) or {}).get(
            "workflow_id", "") or "") or {}
    except Exception:  # noqa: BLE001
        plan = {}
    keys = [str(k) for k in (plan.get("dedupe_keys") or [])]
    if not keys:
        raise RefreshRefused(
            "this plan declares no dedupe_keys, so a re-extraction could not be "
            "matched to an existing record. Writing anyway would risk replacing a "
            "value on the wrong row, so nothing was written.")

    specs = [f for f in (plan.get("fields") or [])
             if isinstance(f, dict) and f.get("name")]
    if not specs:
        raise RefreshRefused("the plan's schema is not stored, so there is nothing to re-extract")

    by_sig: dict[str, dict] = {}
    for rec in records:
        sig = _sig(_plain(rec.get("fields", {})), keys)
        if sig:
            by_sig.setdefault(sig, rec)

    sem = asyncio.Semaphore(2)

    async def _one(src: dict) -> tuple[str, list[dict]]:
        url = str(src.get("url") or "")
        if not url or fetch is None:
            return url, []
        async with sem:
            # Straight to the network. Reuse lives in `crawler.fetch_all`'s
            # queue, which this path never enters, so the stored copy is not a
            # candidate to begin with.
            try:
                page = await fetch(url)
            except Exception:  # noqa: BLE001
                return url, []
        if not isinstance(page, dict) or page.get("skipped") or page.get("error"):
            return url, []
        try:
            text = await asyncio.to_thread(page_evidence_text, page)
        except Exception:  # noqa: BLE001
            return url, []
        working = {**page, "_evidence_text": text}
        from app.services import extractor as extractor_svc
        try:
            recs, _provider = await extractor_svc.extract_page(plan, working, llm)
        except Exception:  # noqa: BLE001
            return url, []
        return url, (recs or [])

    batches = await asyncio.gather(*(_one(s) for s in chosen))

    changed: list[dict] = []
    unchanged = 0
    unverifiable = 0
    unmatched: list[dict] = []
    seen_records: set[str] = set()

    for url, recs in batches:
        for raw in recs or []:
            if not raw.get("fields"):
                continue
            sig = _sig(_plain(raw.get("fields", {})), keys)
            record = by_sig.get(sig) if sig else None
            if record is None:
                unmatched.append({"url": url,
                                  "reason": "no existing record carries this identity"})
                continue
            record_id = str(record.get("record_id") or "")
            if record_id in seen_records:
                # Two refreshed pages describing one entity: the first verified
                # answer is the answer, exactly as in backfill. Re-deciding here
                # would make the outcome depend on which page was fetched first.
                continue
            seen_records.add(record_id)

            budget = validator_svc.JudgeBudget(JUDGE_CALLS_PER_RECORD)
            cells = await validator_svc.wrap_record(
                specs, raw, raw.get("source_text", "") or raw.get("_evidence_text", ""),
                raw.get("source_url", "") or url, raw.get("source_title", ""),
                references=raw.get("references", ""),
                page_id=raw.get("page_id", ""), budget=budget)

            existing = record.get("fields", {})
            for name, cell in cells.items():
                verdict = decide_one(existing.get(name), cell)
                if verdict["action"] == "unchanged":
                    unchanged += 1
                elif verdict["action"] == "unverifiable":
                    unverifiable += 1
                elif verdict["action"] == "empty":
                    # An empty cell is backfill's job, not refresh's.
                    pass
                else:
                    changed.append({
                        "record_id": record_id,
                        "field": name,
                        "cell": verdict["cell"],
                        "page_id": raw.get("page_id", ""),
                        "url": raw.get("source_url", "") or url,
                        "old_value": verdict["old"],
                    })

    written = 0
    if apply:
        for item in changed:
            if store.update_record_cell(dataset_id, item["record_id"],
                                        item["field"], item["cell"]):
                written += 1

    return {
        "dataset_id": dataset_id,
        "applied": bool(apply),
        "sources": len(chosen),
        "urls": [s.get("url", "") for s in chosen],
        "refetched": len(chosen),
        "records_rechecked": len(seen_records),
        "changed": len(changed),
        "written": written,
        "unchanged": unchanged,
        "unverifiable": unverifiable,
        "unmatched": len(unmatched),
        "unmatched_examples": unmatched[:5],
        "judge_calls_used": JUDGE_CALLS_PER_RECORD * len(seen_records),
        "changed_examples": [
            {"record_id": c["record_id"], "field": c["field"],
             "old": c["old_value"], "new": _cell_value(c["cell"])} for c in changed[:5]],
        "refetches": True,
        "writes_only_verified": True,
        "preserves_rivals": True,
        "note": ("sources were re-fetched and values re-verified for real; nothing "
                 "was written" if not apply else
                 "differing values written, each keeping the replaced value as a "
                 "reviewable rival"),
    }


def _same(a: Any, b: Any) -> bool:
    """Value equality, tolerant of the shapes a cell value can take.

    Case and surrounding whitespace are normalised because a page that renders
    "Acme Ltd" and "acme ltd" describes the same company, and reporting that as
    a change would fill the conflict queue with noise. Anything else compares as
    its own normalised string; a non-string value compares by equality.
    """
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()
    return a == b


def decide_one(current: Any, fresh: dict) -> dict:
    """What happens to one cell. The whole write rule, as a pure function.

    Pulled out of the loop deliberately. This is the part that can silently do
    the wrong thing — replacing a proven value with an unproven one, churning
    every cell on a no-op refresh, or quietly taking over backfill's job — and it
    is unreachable through the real validator, whose judge would have to be
    mocked wholesale to observe it. As a function it is four branches with no
    I/O, so each one can be checked directly.

    Returns `{action, cell, old}` where action is one of:

    * ``changed``       — a proven value replacing a different proven one; the
      returned cell carries the old value as a rival.
    * ``unchanged``     — same value. Nothing is written, because rewriting an
      identical cell would churn every cell in the dataset on a no-op refresh.
    * ``unverifiable``  — the new value is empty, or carries no verified quote.
      Fresh is not the same as correct: an unverified value may be right, and it
      may not be written over a proven one.
    * ``empty``         — there was no value here. Backfill owns that cell; doing
      it here would blur which operation actually did the work.
    """
    old = _cell_value(current)
    new = _cell_value(fresh)
    if new in (None, ""):
        return {"action": "unverifiable", "cell": fresh, "old": old}
    if old in (None, ""):
        return {"action": "empty", "cell": fresh, "old": old}
    if _same(old, new):
        return {"action": "unchanged", "cell": fresh, "old": old}
    if str(fresh.get("verification_status") or "") != "verified":
        return {"action": "unverifiable", "cell": fresh, "old": old}
    return {"action": "changed", "cell": _with_rival(current, fresh), "old": old}


def _with_rival(current: Any, new_cell: dict) -> dict:
    """The new cell, carrying the value it replaced as a rival.

    Without this a refresh is a silent overwrite, and the human who has to
    notice that a value changed is left with a dataset that looks exactly as
    trustworthy as before while saying something different.
    """
    out = dict(new_cell)
    rivals = list(out.get("rivals") or [])
    old_cell = current if isinstance(current, dict) else {}
    rivals.append({
        "value": _cell_value(current),
        "source": dict(old_cell.get("source") or {}),
        "verification_status": old_cell.get("verification_status", "unverified"),
        "superseded_by_refresh": True,
    })
    out["rivals"] = rivals
    out["refreshed_from"] = (new_cell.get("source") or {}).get("url", "")
    return out
