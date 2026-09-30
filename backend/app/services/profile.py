"""What each field of a dataset actually looks like, and where the records are.

The dataset page could show a table of records, or a list of fields with
percentages, and neither answers the question someone opens a dataset to ask:
*what is in here, and can I trust it?* Answering that per field needs more
records than a table loads, and computing it in the browser from whatever the
table happened to page in produces numbers that silently disagree with the
Coverage view — the same fields, counted two ways, in the same screen.

So it is computed here, from the same `field_coverage` the Coverage view uses, so
the two cannot drift. Three things come out, and each is omitted when the data
does not support it:

* **shape** — for a numeric field, min/median/max and a histogram; for a
  categorical one, the distinct values and their counts. A field with one
  distinct value gets no chart, because one bar is a sentence with axes and
  drawing it implies a distribution that is not there.
* **trust** — filled/empty/verified/unverified/conflicting per field, taken
  straight from coverage rather than recounted.
* **place** — when a field is recognisably a country or a region, its
  distribution. Not a map projection: an equal-area tile layout over the
  countries, because a hand-drawn world map would be a drawing of a world this
  run never visited.

Nothing here is estimated. A field with no values reports zero and says the
schema declares it; it is never given a default.
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any

from app.services import coverage as coverage_svc

#: Records read for shape. Coverage is exact over all records; shape is a
#: sample, and the caller is told which, because a distribution drawn from the
#: first 2000 of 40,000 records is a claim about those 2000.
MAX_RECORDS = 2000

#: Histogram buckets. Enough to show a shape, few enough that an empty bucket
#: means something. A histogram with 40 buckets is a list of numbers with
#: borders.
BUCKETS = 12

#: Field names that carry a place. Matched on the normalised name, because the
#: schemas that exist name the same idea a dozen ways and none of them is
#: canonical.
_PLACE_HINTS = re.compile(
    r"(country|nation|region|state|province|city|town|location|geography|"
    r"hq|headquarters|based|market|exchange)", re.I)
_PLACE_RANK = (
    ("country", 3), ("nation", 3), ("headquarters", 3), ("hq", 3),
    ("region", 2), ("province", 2), ("state", 2), ("city", 1), ("town", 1),
    ("location", 2), ("geography", 2), ("based", 2),
)

#: Values that are not places even in a field that usually holds them. Without
#: this, `market_cap` matches the "market" hint and the "where" panel reports
#: the distribution of revenue.
_NOT_PLACE = re.compile(
    r"(cap|size|share|price|value|growth|volume|segment|sector_?size|share_?class)",
    re.I)

#: Field names that say their values are points in time. Only these make a bare
#: 4-digit number a year.
_TIME_HINT = re.compile(
    r"(year|yr|date|month|day|since|until|deadline|founded|launched|"
    r"established|incorporated|announced|published|updated|created)", re.I)

_NUM_CLEAN = re.compile(r"[,$\s]")
_TRAILING_UNIT = re.compile(r"\s*(?:%|eur|usd|inr|gbp|million|billion|bn|mn|m|k)$", re.I)

#: Dates, in the shapes these schemas actually contain. A date field charted
#: as a category list is the worst version of this view: a live profile showed
#: `last_verified_date` as 43 separate bars, one per date, which is a wall of
#: noise describing the shape of nothing. Recognised instead, it becomes a range
#: and a distribution over time, which is what the reader wanted.
_DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),
    re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"),
    re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$"),
    re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})$"),
    re.compile(r"^(\d{4})$"),
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _as_date(value: Any, temporal_name: bool = False) -> tuple[int, int, int] | None:
    """(year, month, day) or None. A bare year is accepted, month and day zero.

    `temporal_name` is the field's own name saying so, and a bare 4-digit number
    is only read as a year when it does. Otherwise `market_cap` holding `1500`
    becomes the year 1500 and a revenue histogram turns into a four-century
    timeline. Unambiguous formats — `2024-09-16`, `15 March 2021` — are dates
    whatever the field is called, because nothing else they could be.

    No dateutil, and no guessing beyond these shapes. "roughly 2019" is not a
    date, and coercing it would put a real-looking point on a timeline nobody
    recorded.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    m = _DATE_PATTERNS[0].match(s) or _DATE_PATTERNS[1].match(s)
    if m:
        a, b, c = (int(g) for g in m.groups())
        if a > 31:            # y-m-d
            return (a, b or 1, c or 1)
        return (c, b or 1, a or 1)   # d/m/y
    m = _DATE_PATTERNS[2].match(s)
    if m:
        d, mon, y = m.group(1), m.group(2)[:3].lower(), m.group(3)
        if mon in _MONTHS:
            return (int(y), _MONTHS[mon], int(d))
        return None
    m = _DATE_PATTERNS[3].match(s)
    if m:
        mon, d, y = m.group(1)[:3].lower(), m.group(2), m.group(3)
        if mon in _MONTHS:
            return (int(y), _MONTHS[mon], int(d))
    m = _DATE_PATTERNS[4].match(s)
    if m and temporal_name:
        return (int(m.group(1)), 0, 0)
    return None


def _timeline(dates: list[tuple[int, int, int]]) -> dict:
    if len(dates) < 2:
        return {}
    years = [d[0] for d in dates]
    lo, hi = min(years), max(years)
    if lo == hi:
        return {"min": lo, "max": hi, "by_year": [{"year": lo, "n": len(dates)}]}
    counts = {y: 0 for y in range(lo, hi + 1)}
    for y in years:
        counts[y] = counts.get(y, 0) + 1
    return {
        "min": lo,
        "max": hi,
        "by_year": [{"year": y, "n": n} for y, n in sorted(counts.items())],
    }


def _cell_value(cell: Any) -> Any:
    return cell.get("value") if isinstance(cell, dict) else cell


def _as_number(value: Any) -> float | None:
    """A number, or None. Strings are cleaned of currency, separators and a
    trailing unit; anything still not numeric is not a number.

    Deliberately not clever. "about 40%" and "€1.2bn" are not converted, because
    guessing what a phrase meant is how a histogram ends up with a fabricated
    bucket. Such a value is left out of the shape and still counted as filled,
    which is the honest split between "it has a value" and "it is a number".
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = _TRAILING_UNIT.sub("", value.strip())
    s = _NUM_CLEAN.sub("", s)
    if not s or not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _histogram(values: list[float]) -> dict:
    if len(values) < 2:
        return {}
    lo, hi = min(values), max(values)
    if lo == hi:
        return {}
    width = (hi - lo) / BUCKETS
    counts = [0] * BUCKETS
    for v in values:
        idx = int((v - lo) / width)
        counts[min(BUCKETS - 1, max(0, idx))] += 1
    return {
        "buckets": [
            {"from": round(lo + i * width, 4),
             "to": round(lo + (i + 1) * width, 4),
             "n": n}
            for i, n in enumerate(counts)
        ],
        "min": lo,
        "max": hi,
        "median": median(values),
        "width": width,
    }


def _consolidate(values: list[str], limit: int = 12) -> dict:
    """Counts for a place field, merged on the first comma-separated segment.

    A live profile found `London` (23), `London, England` (14) and `London, UK`
    (4) as three countries of their own, which made a single city look like the
    third most common answer and hid the fact that it was the first by a wide
    margin. Merging on the first segment is a rule, not a guess: it cannot invent
    a place, only notice that two spellings start the same way.

    Both readings are returned, and `variants` says how many raw values were
    collapsed, because "London, Ontario" and "London, UK" also start the same
    way and the reader is entitled to know that this is a first segment and not a
    postcode lookup. The raw counts stay available.
    """
    merged: dict[str, int] = {}
    raw_counts: dict[str, int] = {}
    for v in values:
        k = str(v).strip()
        if not k:
            continue
        raw_counts[k] = raw_counts.get(k, 0) + 1
        head = k.split(",")[0].strip() or k
        merged[head] = merged.get(head, 0) + 1
    raw_sorted = sorted(raw_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "values": [{"value": k, "n": n} for k, n in
                   sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]],
        "truncated": len(merged) > limit,
        "raw": [{"value": k, "n": n} for k, n in raw_sorted[:limit]],
        "variants": len(raw_counts) - len(merged),
        "as_written_count": len(raw_counts),
    }


def _place_rank(name: str) -> int:
    if _NOT_PLACE.search(name):
        return 0
    low = str(name).lower()
    for token, rank in _PLACE_RANK:
        if token in low:
            return rank
    return 0


def profile(store, dataset_id: str, fields: list | None = None,
            max_records: int = MAX_RECORDS) -> dict:
    """Per-field shape and trust, and the best place field if there is one."""
    row = (store.get_dataset_row(dataset_id) or {}) if hasattr(store, "get_dataset_row") else {}
    schema = fields or row.get("schema") or []
    page = store.get_records(dataset_id, "", max(1, min(int(max_records), MAX_RECORDS)), 0) or {}
    records = list(page.get("records") or [])
    total_stored = int(page.get("total") or len(records))
    sampled = total_stored > len(records)

    matrix = coverage_svc.field_coverage(records, schema)
    by_name = {str(f.get("field")): f for f in matrix.get("fields") or []}

    # Values per field, from the sampled records.
    values: dict[str, list[Any]] = {}
    for rec in records:
        for name, cell in (rec.get("fields") or {}).items():
            v = _cell_value(cell)
            if v in (None, "") or not str(v).strip():
                continue
            values.setdefault(str(name), []).append(v)

    out_fields = []
    for entry in by_name.values():
        name = str(entry.get("field"))
        raw = values.get(name, [])
        # The field's own name, asked before the values: a bare `1998` is a year in
        # `founded_year` and a quantity in `market_cap`.
        temporal_name = bool(_TIME_HINT.search(name))
        cov = {
            "present": int(entry.get("present") or 0),
            "missing": int(entry.get("missing") or 0),
            "verified": int(entry.get("verified") or 0),
            "unverified": int(entry.get("unverified") or 0),
            "conflicting": int(entry.get("conflicting") or 0),
            "coverage_pct": entry.get("coverage_pct", 0.0),
            "proven_pct": entry.get("proven_pct", 0.0),
        }
        numbers = [n for n in (_as_number(v) for v in raw) if n is not None]
        numeric = len(numbers) >= 2 and len(numbers) >= 0.6 * len(raw)
        dates = [d for d in (_as_date(v, temporal_name) for v in raw) if d is not None]
        # Dates before numbers: `2026-09-16` is not a number, but a field of
        # years and a field of dates are both numeric-looking and only one of
        # them is a duration.
        temporal = len(dates) >= 2 and len(dates) >= 0.6 * len(raw)

        item = {
            "field": name,
            "records": int(entry.get("records") or 0),
            "values": len(raw),
            "distinct": len({str(v).strip().casefold() for v in raw}),
            "trust": cov,
            # Declared by the schema and empty on every record is a different
            # thing from absent from the schema, and the two call for different
            # work: one is a backfill, the other is a plan that asked for
            # something these sources do not carry.
            "never_extracted": cov["present"] == 0,
            "fully_filled": bool(raw) and cov["missing"] == 0,
        }

        if temporal and dates:
            item["kind"] = "date"
            item["shape"] = _timeline(dates)
        elif numeric:
            item["kind"] = "numeric"
            item["shape"] = _histogram(numbers)
        elif len({str(v).strip().casefold() for v in raw}) >= 2:
            item["kind"] = "categorical"
            counts: dict[str, int] = {}
            for v in raw:
                k = str(v).strip()
                counts[k] = counts.get(k, 0) + 1
            item["shape"] = {
                "values": sorted(
                    ({"value": k, "n": n} for k, n in counts.items()),
                    key=lambda d: (-d["n"], d["value"]))[:12],
                "truncated": len(counts) > 12,
            }
        else:
            # One distinct value, or none. Deliberately chartless.
            item["kind"] = "scalar" if raw else "empty"
            item["shape"] = {}
        out_fields.append(item)

    out_fields.sort(key=lambda f: (-f["trust"]["present"], f["field"]))

    # The place field, if the schema has an unambiguous one.
    place = None
    best = 0
    for item in out_fields:
        rank = _place_rank(item["field"])
        if rank and item["kind"] == "categorical" and rank > best:
            best, place = rank, item

    return {
        "dataset_id": dataset_id,
        "records": total_stored,
        "records_read": len(records),
        "sampled": sampled,
        "fields": out_fields,
        "place": _place_panel(place, values.get(place["field"], []) if place else []),
        "totals": {
            "fields": len(out_fields),
            "never_extracted": sum(1 for f in out_fields if f["never_extracted"]),
            "partial": sum(1 for f in out_fields
                           if 0 < f["trust"]["present"] < f["records"]),
            "complete": sum(1 for f in out_fields if f["fully_filled"]),
            "conflicting": sum(1 for f in out_fields if f["trust"]["conflicting"] > 0),
        },
    }


def _place_panel(place: dict | None, raw_values: list) -> dict | None:
    """The place panel, with the raw readings alongside the merged one.

    Consolidated from every value, not from the field's already-truncated chart:
    merging the top twelve would have reported `London` three times and still
    counted 3 rather than 41.

    Both readings are reported because they disagree, and the disagreement is
    the finding: three spellings of one city in the source data is worth seeing
    whether or not the panel merges them.
    """
    if not place:
        return None
    merged = _consolidate([str(v) for v in raw_values])
    return {
        "field": place["field"],
        "values": merged["values"],
        "truncated": merged["truncated"],
        "raw": merged["raw"],
        "variants": merged["variants"],
        "as_written_count": merged["as_written_count"],
    }
