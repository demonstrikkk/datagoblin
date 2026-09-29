"""Which hosts have ever actually produced records, learned from what was kept.

Discovery has no memory. Every run re-discovers the same websites, and a run
cannot tell the difference between a site that listed forty of the companies you
asked for and a site whose homepage is a cookie banner — both are one successful
fetch and one stored page. The evidence that separates them already exists and is
never read: every verified cell names the page it came from, so the set of hosts
that own verified values *is* the answer.

So this reads that evidence rather than adding a table to maintain. Nothing is
estimated and nothing is written. A host with no cells behind it is reported at
zero rather than given a default, because "we have no evidence this site is
useful" and "this site is useless" are different claims and only the first one is
true.

The ranking bonus is deliberately small and bounded. Yield is a tiebreaker
between hosts that are already plausible — it is not allowed to overrule the
relevance of a page's own text, because the one measured run where relevance
picked the pages (booking.com and kayak.com for a dataset of companies, matching
1 record in 5) showed what a yield signal that dominates the text is worth.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

#: Records read when computing yield. The backfill path already reads records in
#: full for identity matching, so this is not a new class of cost — but it is a
#: cap, and a dataset larger than this would be sampled rather than counted in
#: full. Reported as `sampled` so the caller can say so instead of implying
#: completeness it does not have.
MAX_RECORDS = 2000

#: Ceiling on the bonus yield can add to a page's relevance score. Kept below
#: the difference between "this page names thirty of your companies" and "this
#: page names none", so yield orders the plausible and never rescues the
#: irrelevant.
MAX_BONUS = 3.0


def host_of(url: Any) -> str:
    """The host that produced evidence for a URL, lowercased, or "".

    `www.` is dropped so that the same site reached by two spellings is one host
    rather than two entries that each look half-used.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw).netloc or "").lower()
    except ValueError:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _blank() -> dict:
    return {"records": 0, "cells": 0, "verified": 0, "unverified": 0,
            "conflicting": 0, "sources": 0, "pages": 0, "source_ok": 0}


def _cell_state(cell: Any) -> tuple[Any, str]:
    if not isinstance(cell, dict):
        return (cell, "unverified") if cell not in (None, "") else (None, "missing")
    value = cell.get("value")
    if value in (None, ""):
        return None, "missing"
    return value, str(cell.get("verification_status") or "unverified")


def _record_hosts(rec: dict) -> dict[str, dict[str, int]]:
    """host -> cell counts for one record.

    Verified is counted per state rather than as a flag so a cell whose verdict
    is `conflicting` is reported as a conflict and not quietly as a success —
    a host that supplies only disputed values is a different proposition from
    one that supplies proven ones.
    """
    out: dict[str, dict[str, int]] = {}
    for _name, cell in (rec.get("fields") or {}).items():
        value, state = _cell_state(cell)
        if value is None:
            continue
        src = cell.get("source") if isinstance(cell, dict) else None
        url = (src.get("url") if isinstance(src, dict) else src) or ""
        host = host_of(url)
        if not host:
            continue
        slot = out.setdefault(host, {"cells": 0, "verified": 0, "conflicting": 0})
        slot["cells"] += 1
        if state == "verified":
            slot["verified"] += 1
        elif state == "conflicting":
            slot["conflicting"] += 1
    return out


def build(store, dataset_id: str, records: list[dict] | None = None,
          run_id: str = "") -> dict:
    """Host -> what it produced. Read-only, derived entirely from stored evidence.

    `records` may be passed in by a caller that already has them (the backfill
    path does), which is what keeps this free rather than a second full read.
    """
    hosts: dict[str, dict] = {}

    def slot(host: str) -> dict:
        return hosts.setdefault(host, _blank())

    sampled = False
    if records is None:
        got = store.get_records(dataset_id, "", MAX_RECORDS, 0) or {}
        records = list(got.get("records") or [])
        sampled = bool(got.get("total", len(records)) or 0) > len(records)

    for rec in records:
        rid = str(rec.get("record_id") or "")
        for host, counts in _record_hosts(rec).items():
            row = slot(host)
            row["cells"] += counts["cells"]
            row["verified"] += counts["verified"]
            row["conflicting"] += counts["conflicting"]
            row["unverified"] += max(0, counts["cells"] - counts["verified"]
                                      - counts["conflicting"])
            # A record is counted once per host, not once per cell: fifty fields
            # from one page is one company, and counting it fifty times would
            # make a wide-schema host look like a productive one purely for
            # having more columns.
            row.setdefault("_recs", set())
            row.setdefault("_anon", 0)
            if rid:
                row["_recs"].add(rid)
            else:
                row["_anon"] += 1

    # Sources: what was actually fetched, and how it ended.
    try:
        src_block = store.get_sources(dataset_id) or {}
    except Exception:  # noqa: BLE001
        src_block = {}
    for src in src_block.get("sources") or []:
        host = host_of(src.get("url"))
        if not host:
            continue
        row = slot(host)
        row["sources"] += 1
        if str(src.get("status") or "") in ("ok", "reused"):
            row["source_ok"] += 1

    # Pages: what it cost to find out.
    if not run_id:
        lister = getattr(store, "get_dataset_row", None)
        if callable(lister):
            row_data = lister(dataset_id) or {}
            run_id = str(row_data.get("run_id") or "")
    if run_id:
        try:
            for page in store.get_pages(run_id, 500) or []:
                host = host_of(page.get("url"))
                if host:
                    slot(host)["pages"] += 1
        except Exception:  # noqa: BLE001
            pass

    for row in hosts.values():
        recs = set(row.pop("_recs", set()) or set())
        anon = int(row.pop("_anon", 0) or 0)
        row["records"] = len(recs) + anon
        row["records_per_page"] = (
            round(row["records"] / row["pages"], 3) if row["pages"] else 0.0)
        row["verified_pct"] = (
            round(100 * row["verified"] / row["cells"], 1) if row["cells"] else 0.0)
        # The one number that answers "is this site worth another visit": cells
        # proven, per page spent. A host with no pages has spent nothing and
        # earned nothing, which is a zero rather than an infinity.
        row["yield"] = (
            round(row["verified"] / row["pages"], 3) if row["pages"] else 0.0)

    ordered = sorted(
        hosts.items(),
        key=lambda kv: (-kv[1]["verified"], -kv[1]["records"], kv[0]))
    return {
        "dataset_id": dataset_id,
        "run_id": run_id,
        "records_read": len(records),
        "sampled": sampled,
        "hosts": dict(ordered),
        "ranking": [h for h, _ in ordered],
    }


def bonus(yieldmap: dict, host: str) -> float:
    """0..MAX_BONUS for a host, from its own recorded yield.

    Zero for an unknown host. A host nobody has evidence about gets no benefit
    of the doubt and no penalty either — it is simply unranked, and the relevance
    of its text decides.
    """
    row = ((yieldmap or {}).get("hosts") or {}).get(host) or {}
    raw = float(row.get("yield") or 0.0)
    if raw <= 0:
        return 0.0
    # Saturating: the difference between 0.5 and 1 verified cell per page is
    # worth something, the difference between 20 and 21 is not.
    scaled = MAX_BONUS * (raw / (raw + 2.0))
    return round(min(MAX_BONUS, scaled), 3)


def attach_to_sources(sources: list[dict], yieldmap: dict) -> list[dict]:
    """Per-source yield, for the Sources view.

    Returns copies: the caller may be holding rows that other views render, and
    this adds fields they have no use for.
    """
    hosts = (yieldmap or {}).get("hosts") or {}
    out = []
    for src in sources or []:
        host = host_of(src.get("url"))
        row = dict(src)
        row["host"] = host
        row["yield"] = hosts.get(host) or _blank()
        out.append(row)
    return out
