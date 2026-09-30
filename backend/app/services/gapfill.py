"""Phase two: when re-reading what we already have does not fill a field, go and get more.

Phase one (`backfill`) re-reads the pages a run already stored. It costs LLM calls
but no external requests, which makes it the right first move and the right only
move whenever it can work. It cannot close every gap, though, and the gap it
cannot close is the common one: the pages the run happened to fetch do not
describe the entity at all. A dataset of 88 companies whose `funding_stage` is
filled on 4 of them is not missing an extraction — it is missing 84 pages.

This module is the second phase, and it is the first part of the system that
spends money on the open web on its own initiative. That makes the limits the
important part, not the search:

* **It only runs on a gap that survived phase one.** `gaps.can_attempt` enforces
  the ladder, and this module refuses a gap whose phase is not a predecessor of
  the one asked for. A gap at phase 0 is never searched, because the stored pages
  have not been tried yet and they are free.
* **`evidence_gap` is refused outright.** A value that carries no verdict, or two
  sources that disagree, is not fixed by fetching more. Searching here would
  spend a search to produce a second unproven value for a field whose problem is
  that the first one was never judged. The remedy is the judge or a person, and
  saying so is more useful than an expensive no-op.
* **The write rule is unchanged and is not relaxed here.** Exact identity match
  through the deduper's signature, empty cells only, and every written value goes
  through the same `wrap_record` the run uses. A value written by this path is
  indistinguishable from one written at run time, which is the only way the
  verification contract survives this feature existing.
* **Every attempt is bounded and metered, and its cost is returned.** A search
  that returns nothing costs real money and the caller is told so, whether or not
  anything was written.

The search query is built from the record's own identity rather than the dataset's
subject, because "Series A climate software Berlin" finds the same three
listings for every record and re-adds entities already in the dataset. A query
naming the entity can only return pages about that entity.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from app.core.errors import AppError
from app.services import coverage as coverage_svc
from app.services import extractor as extractor_svc
from app.services import gaps as gaps_svc
from app.services import validator as validator_svc
from app.services.deduper import _sig
from app.services.reducer import page_evidence_text

#: Search results requested per record. Deliberately small: this is a targeted
#: lookup for one entity and one field, not discovery, and discovery is the run's
#: job.
SEARCH_LIMIT_PER_RECORD = 4

#: Ceiling on searches in one phase-2 pass, across all records. The per-record
#: limit alone would let a 1,000-record dataset queue 4,000 searches in one
#: request, so the pass-level cap is the one that actually bounds the bill.
MAX_SEARCHES_PER_PASS = 12

#: Pages fetched per record, after filtering. Filtering matters more than the
#: number: a search for a named company returns its own site, a directory, two
#: news articles and an aggregator, and only the first two are evidence.
MAX_FETCHES_PER_RECORD = 3

#: Judge calls per record, per pass. Same reasoning as `backfill`: a separate,
#: small allowance so a second phase cannot quietly become the most expensive
#: operation in the system.
JUDGE_CALLS_PER_RECORD = 6


class GapFillRefused(Exception):
    """The gap cannot be served at this phase. Reported, never partially applied."""


def _plain(fields: dict) -> dict:
    return {k: (v.get("value") if isinstance(v, dict) else v)
            for k, v in (fields or {}).items()}


def _needles(value: Any) -> list[str]:
    """Quoted fragments of an identity value worth putting in a query.

    A raw company name goes in as-is, but a value that is really a sentence
    ("Acme Corp — Series B, Berlin") is trimmed to its distinctive head, because
    a 300-character search query against a search API matches nothing and still
    costs the request.
    """
    if not isinstance(value, str):
        return []
    t = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 .&'-]+", " ", value)).strip()
    if len(t) < 3:
        return []
    return [t[:120]]


def build_queries(field: str, record: dict, keys: list[str]) -> list[str]:
    """The queries to try for one record's missing field, best first.

    Named-entity queries come first and the bare field query is not used at all
    for a `depth_gap`: a query with no entity in it returns pages about the
    subject in general, which is what the run already did. Those pages describe
    other companies, and attaching their values to this record is the exact
    failure the identity match exists to prevent.
    """
    plain = _plain(record.get("fields", {}))
    out: list[str] = []
    for key in keys:
        for frag in _needles(plain.get(key)):
            q = f'"{frag}" {field}'.strip()
            if q not in out:
                out.append(q)
    return out


def entity_queries(field: str, records: list[dict], keys: list[str],
                   budget: int) -> list[tuple[str, str]]:
    """(record_id, query) pairs, capped at `budget` searches in total.

    Ordered by how much of the record is actually identifiable, so a pass spends
    its searches on records it can actually attach a value to. A record with no
    identity yields no query at all: searching for it would find pages that could
    not be matched to it, so the spend would be pure waste.
    """
    pairs: list[tuple[str, str]] = []
    for rec in sorted(records, key=lambda r: -len(_plain(r.get("fields", {})))):
        rid = str(rec.get("record_id") or "")
        if not rid:
            continue
        for q in build_queries(field, rec, keys):
            pairs.append((rid, q))
            if len(pairs) >= max(1, budget):
                return pairs
    return pairs


def _host(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", url.split("/", 3)[2].lower())
    except (IndexError, AttributeError):
        return ""


def plausible(url: str, entity: str) -> bool:
    """Whether a search result is worth a fetch.

    Two rules, both about not paying for a page that cannot help. A result whose
    host contains the entity's own name is its homepage and is worth reading; a
    result on some other host is only worth reading if its URL or title actually
    mentions the entity. This drops the "10 best Series A climate startups"
    roundups that otherwise dominate the results for a named company and describe
    other companies entirely.
    """
    if not url.startswith(("http://", "https://")):
        return False
    host, low = _host(url), url.lower()
    if not host:
        return False
    slug = re.sub(r"[^a-z0-9]+", "", entity.lower())
    if not slug or len(slug) < 4:
        return False
    # Compare de-punctuated forms. A directory URL spells the company
    # `acme-corp` and a slug spells it `acmecorp`, so matching the raw slug
    # against the raw URL finds nothing and the one page most likely to be about
    # the entity is discarded as irrelevant.
    squashed = re.sub(r"[^a-z0-9]+", "", low)
    if re.sub(r"[^a-z0-9]+", "", host).startswith(slug[:12]):
        return True
    return slug[:10] in squashed


async def run(store, dataset_id: str, field: str, *, phase: int, llm: Any,
              search_fn: Callable[..., Any], fetch_fn: Callable[..., Any],
              persist_page: Callable[[dict], Any] | None = None,
              search_budget: int = MAX_SEARCHES_PER_PASS,
              apply: bool = False) -> dict:
    """Attempt `phase` on one gap and report exactly what it cost and did.

    `apply=False` performs the real search, the real fetch and the real
    extraction, and stops before writing. The proposal's estimate is a guess;
    this is the measurement, and it is the only way to know the estimate was
    wrong before paying for it a second time.

    Returns the gap's post-attempt state alongside the work report, so the caller
    can persist one object rather than reconciling two.
    """
    if phase not in (gaps_svc.PHASE_STORED, gaps_svc.PHASE_SEARCH):
        raise GapFillRefused(f"unknown phase {phase}")

    gap = store.get_gap(dataset_id, field) if hasattr(store, "get_gap") else None
    if gap is None:
        raise GapFillRefused(
            "this field has no recorded gap, so there is nothing to attempt; "
            "open the coverage view first so the gap is derived and recorded")

    allowed, why = gaps_svc.can_attempt(gap, phase=phase)
    if not allowed:
        raise GapFillRefused(why)

    if str(gap.get("category")) == "evidence_gap":
        # Checked after the ladder so the refusal names the more specific reason
        # when both apply, and refused rather than attempted.
        raise GapFillRefused(
            "this is an evidence gap: the values exist but carry no verdict, or "
            "two sources disagree. Fetching more cannot add a verdict, so this "
            "needs the judge or a person — see the Conflicts and Review views")

    if phase == gaps_svc.PHASE_STORED:
        # Phase one already has an implementation, an allowlist of stored pages and
        # its own metering. Delegating keeps one code path for re-reading stored
        # pages rather than a second one that would drift from it.
        from app.services import backfill as backfill_svc
        out = await backfill_svc.run(store, dataset_id, [field], llm=llm,
                                     apply=apply, include_partial=True)
        return _finish(gap, phase, out, {
            "pages_read": int(out.get("pages_read") or 0),
            "cells_written": int(out.get("written") or 0),
            "searches_run": 0, "pages_fetched": 0,
        }, out)

    # --- phase two: search the web ------------------------------------------
    from app.services import backfill as backfill_svc

    plan = backfill_svc.plan_for_dataset(store, dataset_id)
    ds = backfill_svc._dataset_row(store, dataset_id)
    keys = [str(k) for k in (plan.get("dedupe_keys") or [])]
    if not keys:
        raise GapFillRefused(
            "this plan declares no dedupe_keys, so a value found on the web could "
            "not be matched to a record safely. Nothing was searched and nothing "
            "was written")

    records = (store.get_records(dataset_id, "", 1000, 0) or {}).get("records", [])
    if not records:
        raise GapFillRefused("this dataset has no records, so there is nothing to fill")

    targets = [r for r in records
               if _is_empty((r.get("fields") or {}).get(field))]
    if not targets:
        # Nothing to do is a resolution, not a failure: the field is no longer
        # missing anywhere, so there is no gap to attempt.
        return _finish(gap, phase, {
            "records_searched": 0, "searches_run": 0, "pages_fetched": 0,
            "fillable": 0, "written": 0, "skipped_existing": 0, "unmatched": 0,
            "note": "every record already has a value for this field, so the gap "
                    "is closed and no search was needed",
        }, {"pages_read": 0, "cells_written": 0, "searches_run": 0,
            "pages_fetched": 0}, None)

    pairs = entity_queries(field, targets, keys, search_budget)
    if not pairs:
        raise GapFillRefused(
            "none of the records missing this field carry an identifying value, "
            "so there is nothing to search for. Nothing was searched and nothing "
            "was written")

    spec_by_name = {str(f.get("name")): f for f in (plan.get("fields") or [])
                    if isinstance(f, dict) and f.get("name")}
    extract_names = sorted({field} | set(keys))
    extract_specs = [spec_by_name[n] for n in extract_names if n in spec_by_name]
    if field not in spec_by_name:
        raise GapFillRefused("the plan's schema does not describe this field")
    reduced_plan = {**plan, "fields": extract_specs}

    fetched: list[tuple[str, dict]] = []
    searches_run = 0
    search_errors: list[str] = []

    async def _one(pair: tuple[str, str]) -> list[tuple[str, dict]]:
        rid, query = pair
        entity = query.split('"')[1] if '"' in query else query
        try:
            results = await search_fn(query, SEARCH_LIMIT_PER_RECORD)
        except AppError as exc:
            search_errors.append(str(exc)[:120])
            return []
        keep: list[str] = []
        for r in results or []:
            if plausible(str(r.get("url", "")), entity):
                keep.append(str(r["url"]))
        pages: list[tuple[str, dict]] = []
        for url in keep[:MAX_FETCHES_PER_RECORD]:
            try:
                page = await fetch_fn(url, "http")
            except Exception:  # noqa: BLE001 (a dead URL is not a phase failure)
                continue
            if page and (page.get("markdown") or page.get("raw_html") or page.get("html")):
                pages.append((rid, page))
        return pages

    # Sequential on purpose. Search APIs rate-limit aggressively and a burst of
    # twelve parallel queries is the shape most likely to come back 429 for the
    # whole batch, which would waste the entire pass's budget on one refusal.
    for pair in pairs:
        searches_run += 1
        fetched.extend(await _one(pair))

    if search_errors and not fetched:
        return _finish(gap, phase, {
            "records_searched": 0, "searches_run": searches_run, "pages_fetched": 0,
            "fillable": 0, "written": 0, "skipped_existing": 0, "unmatched": 0,
            "search_errors": search_errors[:3],
            "note": "no result could be searched for; the gap is left open and the "
                    "failure is recorded rather than treated as a finding",
        }, {"pages_read": 0, "cells_written": 0, "searches_run": searches_run,
            "pages_fetched": 0}, search_errors[0] if search_errors else "")

    if not fetched:
        return _finish(gap, phase, {
            "records_searched": len({r for r, _ in fetched}) or len(pairs),
            "searches_run": searches_run, "pages_fetched": 0,
            "fillable": 0, "written": 0, "skipped_existing": 0,
            "unmatched": len(pairs),
            "note": f"{searches_run} search(es) returned no page that mentions the "
                    f"entity it was asked about, so nothing was fetched",
        }, {"pages_read": 0, "cells_written": 0, "searches_run": searches_run,
            "pages_fetched": 0}, None)

    # Store before extracting, for the same reason the run does: a quote that
    # cites a page nobody kept cannot be checked afterwards.
    page_ids: dict[int, str] = {}
    for i, (_rid, page) in enumerate(fetched):
        if persist_page is None:
            continue
        try:
            page_ids[i] = await persist_page(page)
        except Exception:  # noqa: BLE001
            page_ids[i] = ""

    by_sig: dict[str, dict] = {}
    for rec in records:
        sig = _sig(_plain(rec.get("fields", {})), keys)
        if sig:
            by_sig.setdefault(sig, rec)

    sem = asyncio.Semaphore(3)
    filled: list[dict] = []
    skipped_existing = 0
    unmatched = 0
    matched: set[str] = set()

    async def _extract(i: int, page: dict) -> list[dict]:
        async with sem:
            working = {**page, "html": "", "markdown": page.get("markdown", "")}
            if not working["markdown"]:
                full = store.get_page(page.get("id", "")) if page.get("id") else None
                if not full:
                    return []
                working = {**full, "html": full.get("raw_html", "") or ""}
            try:
                source_text = await asyncio.to_thread(page_evidence_text, working)
            except Exception:  # noqa: BLE001
                return []
            working["_evidence_text"] = source_text
            try:
                recs, _p = await extractor_svc.extract_page(reduced_plan, working, llm)
            except Exception:  # noqa: BLE001
                return []
            return recs or []

    batches = await asyncio.gather(*(_extract(i, p) for i, (_r, p) in enumerate(fetched)))

    for batch in batches:
        for raw in batch:
            if not raw.get("fields"):
                continue
            sig = _sig(_plain(raw.get("fields", {})), keys)
            record = by_sig.get(sig) if sig else None
            if record is None:
                unmatched += 1
                continue
            record_id = str(record.get("record_id") or "")
            if not record_id or record_id in matched:
                continue
            budget = validator_svc.JudgeBudget(JUDGE_CALLS_PER_RECORD)
            cells = await validator_svc.wrap_record(
                extract_specs, raw, raw.get("source_text", "") or raw.get("_evidence_text", ""),
                raw.get("source_url", ""), raw.get("source_title", ""),
                references=raw.get("references", ""),
                page_id=raw.get("page_id", ""), budget=budget)
            existing = record.get("fields", {})
            for name, cell in cells.items():
                if name != field:
                    continue
                current = existing.get(name)
                if isinstance(current, dict) and current.get("value") not in (None, ""):
                    skipped_existing += 1
                    continue
                if cell.get("value") in (None, ""):
                    continue
                matched.add(record_id)
                filled.append({"record_id": record_id, "field": name, "cell": cell,
                               "url": raw.get("source_url", "")})

    written = 0
    if apply:
        for item in filled:
            if store.update_record_cell(dataset_id, item["record_id"],
                                        item["field"], item["cell"]):
                written += 1

    report = {
        "records_searched": len(pairs),
        "searches_run": searches_run,
        "pages_fetched": len(fetched),
        "fillable": len(filled),
        "written": written,
        "skipped_existing": skipped_existing,
        "unmatched": unmatched,
        "page_urls": [p.get("url", "") for _r, p in fetched],
        "note": ("searched, fetched and extracted for real; nothing was written"
                 if not apply else
                 "values written, each with its own quote and verdict"),
    }
    return _finish(gap, phase, report, {
        "pages_read": len(fetched), "cells_written": written,
        "searches_run": searches_run, "pages_fetched": len(fetched),
    }, None)


def _is_empty(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return cell in (None, "")
    return cell.get("value") in (None, "")


def _finish(gap: dict, phase: int, report: dict, stats: dict, error: str) -> dict:
    """The attempt's report and the gap's post-attempt state, in one object.

    `stats["cells_written"]` is what decides resolved-vs-exhausted, so it is read
    from the report rather than recomputed: a dry run writes nothing, and a dry
    run that found three fillable cells has not closed the gap. Passing the
    measured number in means the two can never disagree.
    """
    if not error and int(stats.get("cells_written") or 0) == 0 and int(
            report.get("fillable") or 0) > 0 and not report.get("applied"):
        # A dry run that found values but wrote none has not learned whether the
        # sources answer; it has learned which ones. Recording it as exhausted
        # would close a gap on the strength of a measurement that was never
        # applied, and the next pass would refuse to search for it.
        error = (f"dry run: {report['fillable']} cell(s) would fill, so this gap "
                 f"is left open for an applied pass")
    after = gaps_svc.record_attempt(gap, phase=phase, stats=stats, error=error)
    return {"phase": phase, "report": report, "gap": after, "stats": stats}
