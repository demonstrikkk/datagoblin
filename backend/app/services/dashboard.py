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

import threading
import time

from app.core.logging import log
from app.services import coverage as coverage_svc
from app.services import sector as sector_svc
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

#: Window in which a concurrent caller waits for the compute already in flight
#: rather than starting its own.
#:
#: Without this the cache only helps *after* the first request finishes. The
#: dashboard is the landing page, so the first thing anyone does after a
#: workspace is empty is open it three ways at once — and three simultaneous
#: cold reads meant three identical 20-30s computes against the same database,
#: which is how a slow page also becomes a slow database. The stampede is the
#: expensive case and it is the one the TTL does not cover.
#:
#: Deliberately short: it bounds how long a waiter blocks, not how long a result
#: is fresh, so it does not interact with CACHE_TTL_S. A crashed compute cannot
#: wedge the cache either, because the event is always released.
SINGLE_FLIGHT_S = 120

_cache: dict = {}
_inflight: dict = {}
_lock = threading.Lock()


def invalidate() -> None:
    """Drop the cached aggregate. Called whenever the data behind it changes."""
    with _lock:
        _cache.clear()


def _single_flight(key: str, factory):
    """Run `factory` once per `key`; concurrent callers block on the same result.

    Returns (value, shared) where `shared` says this caller waited on somebody
    else's compute rather than doing its own.

    Threads rather than asyncio on purpose. The compute is synchronous and was
    measured at 20-30s against a remote database, and the route runs it through
    `asyncio.to_thread`. An asyncio-based version would have to move the compute
    onto the event loop to be able to await a shared future, which would block
    every other request in the process for the duration — a slower way of fixing
    a stampede than causing one. This keeps the work off the loop, and the waiter
    blocks a worker thread it would otherwise have spent computing the identical
    answer.

    The lock is only ever held for dict operations, never across `factory`, so a
    second caller is not serialised behind the first on the lock itself.
    """
    with _lock:
        pending = _inflight.get(key)
        if pending is None:
            pending = {"done": threading.Event(), "value": None,
                       "error": None, "shared": False}
            _inflight[key] = pending
            leader = True
        else:
            leader = False

    if not leader:
        # Bounded so a compute that hangs cannot pin a worker thread forever. On
        # timeout the waiter does the work itself rather than failing: two
        # computes are wasteful, but a request that never returns is not an
        # answer at all.
        if not pending["done"].wait(timeout=SINGLE_FLIGHT_S):
            return factory(), True
        if pending["error"] is not None:
            raise pending["error"]
        return pending["value"], True

    try:
        value = factory()
    except BaseException as exc:  # noqa: BLE001 (re-raised for every waiter too)
        with _lock:
            _inflight.pop(key, None)
        pending["error"] = exc
        pending["done"].set()
        raise
    with _lock:
        _inflight.pop(key, None)
    pending["value"] = value
    pending["done"].set()
    return value, False


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

    Cached briefly by default, and single-flighted so that a cold dashboard
    opened several ways at once costs one compute rather than one per tab.
    `use_cache=False` forces a recompute, which is what a caller wants after a
    write and what the cache's own invalidation does anyway.

    Synchronous, and called through `asyncio.to_thread`: the compute is
    synchronous and slow, and putting it on the event loop would stall every
    other request in the process.
    """
    if use_cache:
        hit = _cache.get("value")
        if hit is not None:
            age = time.time() - hit["at"]
            if hit["limit"] == limit and age < CACHE_TTL_S:
                out = dict(hit["data"])
                out["cached_age_s"] = round(age, 1)
                out["compute_ms"] = hit["compute_ms"]
                out["shared"] = False
                return out

    def _do() -> dict:
        started = time.time()
        out = _compute(store, limit)
        compute_ms = int((time.time() - started) * 1000)
        out["cached_age_s"] = 0.0
        out["compute_ms"] = compute_ms
        with _lock:
            _cache["value"] = {"at": time.time(), "limit": limit,
                               "data": dict(out), "compute_ms": compute_ms}
        return out

    # Keyed on the limit and on whether the caller opted out of the cache, because
    # two callers asking different questions must not share one answer.
    out, shared = _single_flight(f"dashboard:{limit}:{int(use_cache)}", _do)
    out = dict(out)
    out["shared"] = shared
    return out


def _aggregate_fields(agg: dict) -> dict:
    """Per-field aggregate -> the verdict totals the dashboard reports.

    Same arithmetic as `_verdict_totals`, fed from a different source. The two must
    agree exactly: one computes over records read into this process and one
    computes over the database, and a dashboard whose coverage number depends on
    which adapter is live would be lying about the same data.
    """
    fields = list((agg.get("fields") or {}).values())
    cells = present = missing = verified = unverified = conflicting = 0
    empty = partial = 0
    for f in fields:
        n = int(f.get("records") or 0)
        p = int(f.get("present") or 0)
        cells += p + int(f.get("missing") or 0)
        present += p
        missing += int(f.get("missing") or 0)
        verified += int(f.get("verified") or 0)
        unverified += int(f.get("unverified") or 0)
        conflicting += int(f.get("conflicting") or 0)
        if p == 0:
            empty += 1
        elif 0 < p < n:
            partial += 1
    return {"fields": len(fields), "cells": cells, "present": present,
            "missing": missing, "verified": verified, "unverified": unverified,
            "conflicting": conflicting, "empty_fields": empty,
            "partial_fields": partial}


def _partial_runs(store, run_ids: list[str]) -> dict:
    """Which of these runs finished partially, keyed by run id.

    One read per distinct run rather than per dataset, because datasets share runs
    and the answer is a property of the run. A run that has been reaped reads as
    `False` rather than raising: the dataset it produced is still on disk, and its
    absence from this map must not be reported as "complete".
    """
    out: dict = {}
    for rid in {r for r in run_ids if r}:
        try:
            run = store.get_run(rid) or {}
            out[rid] = bool(run.get("partial")) or str(run.get("status") or "") == "PARTIAL"
        except Exception:  # noqa: BLE001
            out[rid] = False
    return out


def _compute(store, limit: int) -> dict:
    rows = store.list_datasets() or []
    datasets: list[dict] = []
    totals = {"datasets": 0, "records": 0, "cells": 0, "present": 0, "missing": 0,
              "verified": 0, "unverified": 0, "conflicting": 0, "sources": 0,
              "source_ok": 0, "empty_fields": 0, "partial_fields": 0}
    top_hosts: dict[str, dict] = {}
    unread = 0

    live = [r for r in rows[:limit] if str(r.get("id") or "")]

    # One aggregate for every dataset on the page, rather than one records read
    # per dataset. This is the difference between 23s and one round trip over the
    # wire, and it exists because the answer is a dozen integers per dataset that
    # were being computed from megabytes of JSON in this process.
    #
    # Coverage is therefore read *over every record* rather than over a
    # `RECORD_SAMPLE` prefix. That is a change in the number, and the better one:
    # a sample silently understated coverage on any dataset larger than the sample
    # and there was no way to tell from the payload. `sampled` is now false
    # because nothing is sampled.
    aggregates: dict = {}
    aggregator = getattr(store, "coverage_aggregates", None)
    if callable(aggregator):
        try:
            aggregates = aggregator([str(r.get("id")) for r in live]) or {}
        except Exception as exc:  # noqa: BLE001
            # A failed aggregate must not empty the dashboard. The per-dataset path
            # below is slower and computes the same thing, so falling back is a
            # latency regression rather than a correctness one.
            log.warning("dashboard coverage aggregate failed, falling back per dataset: %s", exc)
            aggregates = {}

    # Whether any run behind these datasets was partial, read once for the page
    # rather than once per dataset.
    partial_runs = _partial_runs(store, [str(r.get("run_id") or "") for r in live])

    for row in live:
        did = str(row.get("id") or "")
        if not did:
            continue
        totals["datasets"] += 1
        sampled = False

        # Whether the run behind this dataset finished. The dataset page reads it
        # from the run, and the dashboard renders a "partial catch" pill for it —
        # a pill that could never light up, because nothing here supplied the
        # field. A run that has been reaped is not a reason to drop the dataset
        # that is still on disk.
        partial = partial_runs.get(str(row.get("run_id") or ""), False)

        agg = aggregates.get(did)
        v = _aggregate_fields(agg) if agg else None

        # Records are only read when the aggregate was unavailable, or when the
        # yield ranking needs the documents it can see.
        recs: list[dict] = []
        if v is None or agg is None:
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
                                 "run_id": str(row.get("run_id") or ""),
                                 "sector": None,  # unreadable records carry no reading
                                 "created_at": row.get("created_at") or "",
                                 "partial": partial,
                                 "records": int(row.get("record_count") or 0),
                                 "coverage": None, "sampled": sampled,
                                 "reason": "its records could not be read"})
                continue
            v = _verdict_totals(matrix)
            agg = {"records": len(recs), "fields": {}}
        else:
            sampled = False

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
            "created_at": row.get("created_at") or "",
            "partial": partial,
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
            # What kind of dataset this is, with the receipt. Read from the
            # schema and adjudicated with the records the aggregate has already
            # read, so it costs one dict per field rather than a second pass over
            # the store — this page is cached for 300s and measured at 9ms warm,
            # and it must not become the slow one. `sector` is None when the
            # evidence is thin, and the roll-up counts that rather than guessing.
            "sector": sector_svc.sector_for_records(
                row.get("schema") or [], recs, goal=str(row.get("name") or "")),
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
