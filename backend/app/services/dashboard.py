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

from app.services import coverage as coverage_svc
from app.services import yieldmap as yieldmap_svc

#: Datasets read for the aggregate. Matches the library's own page size, so the
#: dashboard summarises what the user can see rather than more.
MAX_DATASETS = 25

#: Records read per dataset for its coverage. Coverage over a sample is labelled
#: as such; it is not presented as the dataset's coverage.
RECORD_SAMPLE = 2000


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


def summarise(store, limit: int = MAX_DATASETS) -> dict:
    """The whole installation, read once. Writes nothing."""
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

        try:
            ym = yieldmap_svc.build(store, did, recs)
            for host, hrow in (ym.get("hosts") or {}).items():
                slot = top_hosts.setdefault(host, {"host": host, "records": 0,
                                                   "verified": 0, "pages": 0,
                                                   "yield": 0.0})
                slot["records"] += hrow.get("records", 0)
                slot["verified"] += hrow.get("verified", 0)
                slot["pages"] += hrow.get("pages", 0)
                slot["yield"] = hrow.get("yield", 0.0)
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
