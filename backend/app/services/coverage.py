"""What a dataset is missing, and what is genuinely in dispute.

Two questions the UI kept forcing people to answer by hand, and which the
stored shape could support but nothing computed:

* **Coverage** — per field, how many records actually carry a value, split by
  how that value earned its keep (`verified` / `unverified` / `conflicting` /
  absent). A schema listing 15 fields says nothing about whether any of them
  were filled in.
* **Backlog** — the same counts, but ordered by how much work each missing
  field represents, so "15 fields declared, 3 of them empty everywhere" becomes
  an actionable list instead of a surprise at export time.

Everything here is derived by reading. Nothing is inferred, guessed, or
filled in: a field with no value is reported absent, never defaulted. The one
write path (`resolve_conflict`) records a human decision, and it refuses to
invent a value that was never extracted.
"""
from __future__ import annotations

from typing import Any

#: Cell states a stored value can be in. `missing` is ours, not the extractor's:
#: there is no cell at all.
_PRESENT_STATES = ("verified", "unverified", "conflicting", "not_proven")


def _cell_value(cell: Any) -> tuple[Any, str]:
    """(value, state) for a field entry, tolerating the pre-Phase-0 shapes."""
    if not isinstance(cell, dict):
        # Plain scalars were stored before provenance existed. Treat as a value
        # with no verdict, not as a missing field.
        return (cell, "unverified") if cell not in (None, "") else (None, "missing")
    value = cell.get("value")
    if value in (None, ""):
        return None, "missing"
    return value, str(cell.get("verification_status") or "unverified")


def _field_name(entry: Any) -> str:
    """Schema entries are objects (`{"name": ..., "type": ...}`), not strings.

    Taking them as strings put a dict into a set and took the endpoint down on
    every real dataset — the schema has never been a list of bare names.
    """
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("field") or "")
    return str(entry or "")


def field_coverage(records: list[dict], fields: list[str]) -> dict:
    """Per-field fill and verdict counts over `records`.

    `fields` is the dataset schema, so a declared-but-never-extracted field
    still appears — that gap is exactly what this view exists to show.
    """
    total = len(records)
    names: list[str] = [_field_name(f) for f in (fields or [])]
    names = [n for n in names if n]
    seen: set[str] = set(names)
    for r in records:
        for k in (r.get("fields") or {}):
            if k not in seen:
                seen.add(k)
                names.append(k)

    out = []
    for name in names:
        counts = {s: 0 for s in (*_PRESENT_STATES, "missing")}
        values = 0
        for r in records:
            _, state = _cell_value((r.get("fields") or {}).get(name))
            counts[state if state in counts else "unverified"] += 1
            if state != "missing":
                values += 1
        out.append({
            "field": name,
            "records": total,
            "present": values,
            "missing": counts["missing"],
            "verified": counts["verified"],
            "unverified": counts["unverified"] + counts["not_proven"],
            "conflicting": counts["conflicting"],
            # A record with a value but no verdict is not coverage. Reporting it
            # as filled would let a field look complete while every value in it
            # is unproven.
            "coverage_pct": round(100 * values / total, 1) if total else 0.0,
            "proven_pct": round(100 * counts["verified"] / total, 1) if total else 0.0,
        })
    return {"records": total, "fields": out}


def collect_conflicts(records: list[dict]) -> list[dict]:
    """Every disputed cell, with the incumbent and each rival's evidence.

    `rivals` is what the deduper preserved on merge and the judge step may
    have left untouched. Surfacing it is the difference between "this dataset
    has 8 conflicts" as a number and eight decidable questions.
    """
    out = []
    for i, r in enumerate(records):
        rid = r.get("record_id")
        for name, cell in (r.get("fields") or {}).items():
            if not isinstance(cell, dict) or cell.get("verification_status") != "conflicting":
                continue
            rivals = []
            for j, rv in enumerate(cell.get("rivals") or []):
                if not isinstance(rv, dict):
                    continue
                src = rv.get("source") or {}
                rivals.append({
                    "index": j,
                    "value": rv.get("value"),
                    "quote": src.get("quote", ""),
                    "url": src.get("url", ""),
                    "page_id": src.get("page_id", ""),
                })
            out.append({
                "record_id": rid or f"local:{i}",
                "record_index": i,
                "field": name,
                "incumbent": {
                    "value": cell.get("value"),
                    "quote": (cell.get("source") or {}).get("quote", ""),
                    "url": (cell.get("source") or {}).get("url", ""),
                    "page_id": (cell.get("source") or {}).get("page_id", ""),
                },
                "rivals": rivals,
                "decided": False,
            })
    return out


def build_backlog(coverage: dict, conflicts: list[dict]) -> dict:
    """Rank the outstanding work, cheapest-to-close first is wrong — biggest first.

    A field missing from every record is a schema problem: no amount of
    re-reading the pages the run already fetched will fill it. A field missing
    from a few records is a crawl-depth problem. The queue says which is which,
    because the fix is different and guessing costs a full re-run.
    """
    rows = []
    for f in coverage.get("fields", []):
        if f["missing"] == 0 and f["conflicting"] == 0 and f["unverified"] == 0:
            continue
        if f["present"] == 0:
            reason = ("never extracted from any stored page — the schema asks "
                      "for something these sources do not carry")
        elif f["missing"] == f["records"]:
            reason = "never extracted from any record"
        elif f["missing"]:
            reason = f"absent on {f['missing']} of {f['records']} records — a depth or source-coverage gap"
        elif f["conflicting"]:
            reason = f"{f['conflicting']} record(s) disagree between sources"
        else:
            reason = f"{f['unverified']} value(s) have no verdict"
        rows.append({
            "field": f["field"],
            "reason": reason,
            "missing": f["missing"],
            "unverified": f["unverified"],
            "conflicting": f["conflicting"],
            "outstanding": f["missing"] + f["unverified"] + f["conflicting"],
            "routable": bool(f["missing"]) and f["present"] > 0,
        })
    rows.sort(key=lambda r: (-r["outstanding"], r["field"]))
    return {
        "items": rows,
        "open_conflicts": sum(1 for c in conflicts if not c["decided"]),
    }


def apply_resolution(cell: dict, choice: str, rival_index: int | None) -> dict:
    """Return the cell after a human decision, or raise ValueError.

    Only two moves exist, and neither invents data: keep what was extracted and
    stand behind it, or adopt a rival that was actually extracted from a
    different source. There is no "type the right answer here" — that would put
    unquoted text into a dataset whose entire claim is that every value carries
    evidence.
    """
    if not isinstance(cell, dict):
        raise ValueError("not a provenance-bearing cell")
    rivals = cell.get("rivals") or []
    if choice == "keep":
        out = dict(cell)
        out["verification_status"] = "verified"
        out["resolved"] = "kept_incumbent"
        return out
    if choice == "adopt":
        if rival_index is None or not 0 <= int(rival_index) < len(rivals):
            raise ValueError("rival_index out of range")
        rival = rivals[int(rival_index)]
        # Rivals are stored exactly as cells are: {"value":..., "source":{...}}.
        # Reading `quote` off the top level found nothing, so adopting a rival
        # replaced the value and left the cell pointing at the *incumbent's*
        # page — the dataset would claim evidence it did not have.
        src = rival.get("source") or {}
        quote = src.get("quote", rival.get("quote", ""))
        url = src.get("url", rival.get("url", ""))
        page_id = src.get("page_id", rival.get("page_id", ""))
        out = dict(cell)
        out["value"] = rival.get("value")
        out["source"] = {"quote": quote, "url": url, "page_id": page_id}
        out["verification_status"] = "verified"
        out["resolved"] = "adopted_rival"
        return out
    raise ValueError(f"unknown choice: {choice!r}")
