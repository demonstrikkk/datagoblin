"""One page that says what this installation actually contains.

The library lists datasets and the run list lists runs, and neither answers the
question a returning user opens the app to ask: *is any of this working?* That
question is answerable from what is already stored, and answering it by
scraping four pages into a browser is both slow and dishonest — a dashboard that
recomputed coverage client-side would show different numbers than the API and
would show a loading state where the answer is one query away.

So this is a read-only aggregate. Every number is derived from stored evidence
using the same functions the per-dataset views use, which means the dashboard
and the dataset page cannot disagree. Where a number cannot be computed cheaply
it is reported as null with a reason rather than approximated.

Datasets are bounded (see `MAX_DATASETS`) because the aggregate reads records,
and a dashboard that reads every record of every dataset on every load would
turn a summary page into the most expensive request in the product. The cap is
returned in the payload so the header can say what it is looking at.
"""
from __future__ import annotations

import time

from app.services import coverage as coverage_svc
from app.services import yieldmap as yieldmap_svc

#: Datasets read for the aggregate. Matches the library's own page size, so the
#: dashboard summarises what the user can see rather than more.
MAX_DATASETS = 25

#: Records read per dataset for its coverage. Coverage over a sample is labelled
#: as such; it is not presented as the dataset's coverage.
#:
#: Lowering this was tried and did not help: 500 measured 14.5s against 13.7s at
#: 2000, so the cost is not rows. It is the round trips — list_datasets plus a
#: few per dataset, each a network hop to a pooler on the other side of the
#: planet. Fewer rows would only have made the numbers less true for nothing, so
#: the sample stays at the full dataset size and the latency is handled by the
#: cache instead, where it belongs.
RECORD_SAMPLE = 2000

#: How long a computed aggregate is reused, in seconds.
#:
#: The aggregate reads records for every dataset, and against a remote database
#: that measured 31s cold for 15 datasets. Recomputing the identical answer on
#: every navigation is waste rather than freshness, so it is cached — and the
#: window is generous because every write that could change the answer drops the
#: cache itself: a terminal run event, an applied backfill, an applied refresh.
#: A short TTL on top of that would only add recomputes.
CACHE_TTL_S = 300

_cache: dict = {}


def invalidate() -> None:
    """Drop the cached aggregate. Called whenever the data behind it changes."""
    _cache.clear()


def _verdict_totals(matrix: dict) -> dict:
    fields = matrix.get("fields") or []
    return {
        "fields": len(fields),
        "cells": sum(int(f.get("present") or 0) + int(f.get("missing") or 0)
                     for f in fields),
        "present": sum(int(f.get("present") or 0) for f in fields),
        "missing": sum(int(f.get("missing") or 0) for f in fields),
        "verified": sum(int(f.get("verified") or 0) for f in fields),
        "unverified": sum(int(f.get("unverified") or 0) for f in fields),
        "conflicting": sum(int(f.get("conflicting") or 0) for f in fields),
        "empty_fields": sum(1 for f in fields if int(f.get("present") or 0) == 0),
        "partial_fields": sum(1 for f in fields
                              if 0 < int(f.get("present") or 0)
                              < int(f.get("records") or 0)),
    }


def summarise(store, limit: int = MAX_DATASETS, use_cache: bool = True) -> dict:
    """The whole installation, read once. Writes nothing.

    Cached briefly by default. `use_cache=False` forces a recompute, which is
    what a caller wants after a write and what the cache's own invalidation does
    anyway.
    """
    if use_cache:
        hit = _cache.get("value")
        if hit is not None:
            age = time.time() - hit["at"]
            if hit["limit"] == limit and age < CACHE_TTL_S:
                out = dict(hit["data"])
                out["cached_age_s"] = round(age, 1)
                out["compute_ms"] = hit["compute_ms"]
                return out

    started = time.time()
    out = _compute(store, limit)
    compute_ms = int((time.time() - started) * 1000)
    out["cached_age_s"] = 0.0
    out["compute_ms"] = compute_ms
    _cache["value"] = {"at": time.time(), "limit": limit,
                       "data": dict(out), "compute_ms": compute_ms}
    return out


def _compute(store, limit: int) -> dict:
    rows = store.list_datasets() or []
    datasets: list[dict] = []
    totals = {"datasets": 0, "records": 0, "cells": 0, "present": 0, "missing": 0,
              "verified": 0, "unverified": 0, "conflicting": 0, "sources": 0,
              "source_ok": 0, "empty_fields": 0, "partial_fields": 0}
    top_hosts: dict[str, dict] = {}
    unread = 0

    for row in rows[:limit]:
        did = str(row.get("id") or "")
        if not did:
            continue
        totals["datasets"] += 1
        records = (store.get_records(did, "", RECORD_SAMPLE, 0) or {})
        recs = list(records.get("records") or [])
        sampled = bool(records.get("total", len(recs)) or 0) > len(recs)
        try:
            matrix = coverage_svc.field_coverage(recs, row.get("schema") or [])
        except Exception:  # noqa: BLE001
            # One unreadable dataset must not take the whole dashboard down. It
            # is counted and named as unread rather than silently dropped.
            unread += 1
            totals["records"] += int(row.get("record_count") or 0)
            datasets.append({"dataset_id": did, "name": row.get("name", ""),
                             "records": int(row.get("record_count") or 0),
                             "coverage": None, "sampled": sampled,
                             "reason": "its records could not be read"})
            continue

        v = _verdict_totals(matrix)
        totals["records"] += int(row.get("record_count") or 0)
        for k in ("cells", "present", "missing", "verified", "unverified",
                  "conflicting", "empty_fields", "partial_fields"):
            totals[k] += v[k]

        try:
            src = store.get_sources(did) or {}
            totals["sources"] += len(src.get("sources") or [])
            totals["source_ok"] += sum(
                1 for s in (src.get("sources") or [])
                if str(s.get("status") or "") in ("ok", "reused"))
        except Exception:  # noqa: BLE001
            pass

        # Light mode: the summary ranks hosts by proven values, which the records
        # already prove. The full yield also needs sources and pages, and those
        # two queries per dataset were the bulk of this aggregate's cost over a
        # remote database — for a rate the summary does not display.
        try:
            ym = yieldmap_svc.build(store, did, recs, light=True)
            for host, hrow in (ym.get("hosts") or {}).items():
                slot = top_hosts.setdefault(host, {"host": host, "records": 0,
                                                   "verified": 0, "pages": 0,
                                                   "yield": 0.0,
                                                   "cost_unknown": True})
                slot["records"] += hrow.get("records", 0)
                slot["verified"] += hrow.get("verified", 0)
        except Exception:  # noqa: BLE001
            pass

        datasets.append({
            "dataset_id": did,
            "name": row.get("name", ""),
            "run_id": str(row.get("run_id") or ""),
            "records": len(recs),
            "declared_records": int(row.get("record_count") or 0),
            "sampled": sampled,
            "fields": v["fields"],
            "coverage_pct": (
                round(100 * v["present"] / v["cells"], 1) if v["cells"] else 0.0),
            "proven_pct": (
                round(100 * v["verified"] / v["cells"], 1) if v["cells"] else 0.0),
            "conflicting": v["conflicting"],
            "empty_fields": v["empty_fields"],
            "partial_fields": v["partial_fields"],
            "coverage": v,
        })

    # The number worth putting at the top, and the one that is easiest to make
    # flattering: proven cells over all cells. Unverified is reported beside it
    # rather than folded in, because a dataset where every value has a quote and
    # no verdict looks complete and is not.
    proven_pct = (round(100 * totals["verified"] / totals["cells"], 1)
                  if totals["cells"] else 0.0)
    ranked = sorted(top_hosts.values(),
                    key=lambda h: (-h["verified"], -h["records"], h["host"]))

    return {
        "totals": {**totals, "proven_pct": proven_pct},
        "datasets": datasets,
        "top_hosts": ranked[:15],
        "unreadable_datasets": unread,
        "datasets_available": len(rows),
        "datasets_summarised": len(datasets),
        # Stated so the header can be honest about what it is showing rather
        # than implying the whole library when it is the first page of it.
        "truncated": len(rows) > limit,
        "limit": limit,
    }
