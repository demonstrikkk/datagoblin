"""Records from a JSON payload, read without a model.

## Why this exists

A surprising share of useful pages are not pages. They are endpoints:

    https://api.example.com/v2/companies?limit=200
    https://www.somesite.com/data/employees.json
    https://export.example.org/deals.csv

`source_router.triage_source` already recognises these — anything with `/api` in
the path or a `.json` extension returns `structured:api`. But `fetch_all` then
sends the body through the same reducer and extractor as an HTML page, and the
extractor asks a language model to read what is already a list of objects.

That is expensive in three separate ways. It costs a model call per page. It
costs latency on the slowest path in the pipeline. And it is *unreliable*: a
model asked to read `{"revenue": 4200000}` will sometimes return `"$4.2M"`,
sometimes `"4,200,000"`, sometimes `"approximately 4.2 million"`. A JSON parser
cannot.

So this is a ladder rung, not a replacement for the extractor. It sits at the
same level as `jsonld` and `labelled`: deterministic, quote-backed, and
verified by construction because the quote *is* the value as it appears in the
document.

## What it does and does not claim

The matcher is intentionally conservative, and every claim it makes is checked
later by the same gate as everything else:

* A field is filled only when the payload contains a key that matches the
  requested field by normalised name, synonym, or type-compatible path. A field
  with no matching key is left missing, and the model rung is still asked for it.
* The quote is the raw serialised value, so `locate_quote` resolves against real
  document text.
* An array of objects yields one record per element. An array of scalars yields
  one record with a single field.
* A single object yields one record.

## Rejected payloads

`application/json` is a content type, not a shape. These are refused and left
for the model, because treating them as records produces confident nonsense:

    null, "", [], {}            nothing to read
    {"error": "..."}            an error envelope
    a 10 MB array               a payload no caller asked for a row-level read of
    deeply nested trees         the shape that says "this is not a record list"
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services import confidence as conf_svc

#: Refuse payloads larger than this. A record-list rung that happily walks a 40 MB
#: export is not helping; it is the reason a run hit its runtime wall.
MAX_BYTES = 2_000_000

#: Refuse more rows than this for the same reason — and because a caller asking
#: for 15 pages does not mean "give me 900,000".
MAX_RECORDS = 500

#: Keys whose presence means the payload is an envelope, not data.
_ENVELOPE_KEYS = frozenset({"error", "errors", "message", "detail", "trace_id",
                            "traceid", "status", "status_code", "exception"})

#: Below this nesting depth, a value is treated as a leaf worth quoting.
MAX_DEPTH = 4

#: Word-to-word synonyms for requested field names. Keys are matched
#: normalised (lowercase, non-alphanumerics collapsed to single spaces), so
#: `annual_revenue`, `Annual Revenue` and `annual-revenue` are one key.
_ALIASES_RAW: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "revenues", "annualrevenue", "totalrevenue",
                "turnover", "sales", "grossrevenue", "arr", "mrr"),
    "profit": ("profit", "netprofit", "netincome", "earnings", "ebitda",
               "operatingincome", "grossprofit"),
    "headcount": ("headcount", "employees", "employee_count", "employeecount",
                  "numemployees", "size", "staff", "team", "people"),
    "funding": ("funding", "raised", "fundingamount", "totalfunding",
                "amountraised", "lastfunding", "totalraised"),
    "funding_round": ("round", "fundinground", "series", "lastround",
                      "fundingstage", "seriesstage"),
    "valuation": ("valuation", "val", "marketcap", "marketvalue",
                  "postmoney", "premoney", "enterprisevalue"),
    "founded": ("founded", "foundedyear", "yearfounded", "established",
                "since", "inception"),
    "location": ("location", "city", "headquarters", "hq", "address", "country",
                 "region", "basedin"),
    "website": ("website", "url", "homepage", "site", "domain", "link"),
    "industry": ("industry", "sector", "vertical", "category", "market"),
    "ceo": ("ceo", "chiefexecutive", "founder", "founders", "owner",
            "chiefexecutiveofficer"),
    "company_name": ("name", "company", "companyname", "organisation",
                     "organization", "title", "employer"),
    "description": ("description", "summary", "about", "blurb", "tagline",
                    "overview", "pitch"),
    "email": ("email", "emailaddress", "contactemail", "mail"),
    "phone": ("phone", "telephone", "phonenumber", "mobile", "tel"),
}

def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _compact(text: object) -> str:
    """`"Company Name"` -> `"companyname"`. The form the alias table is keyed by."""
    return _norm(text).replace(" ", "")


#: The table above is written with the field names as they appear in a plan.
#: Re-keyed on the compact form so lookup does not have to guess which of
#: `company_name` / `companyName` / `Company Name` the caller used. Written as a
#: comprehension rather than by hand because a key that exists in one form and
#: not the other is a silent miss, and that is the bug this fixes.
_ALIASES: dict[str, tuple[str, ...]] = {
    _compact(k): tuple(v) for k, v in _ALIASES_RAW.items()
}


def looks_like_json(text: object) -> bool:
    """Cheap gate, used before the body is parsed at all."""
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()[:1]
    return stripped in ("{", "[")


def parse_records(text: str, max_records: int = MAX_RECORDS) -> list[dict]:
    """Turn a JSON body into records, or return [] when it is not one.

    Returning [] rather than raising is deliberate: every caller has a model rung
    behind this one, and "this payload is not a record list" is an ordinary
    outcome, not an error.
    """
    if not isinstance(text, str) or not looks_like_json(text):
        return []
    if len(text) > MAX_BYTES:
        return []
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return []

    rows = _rows_from(payload, depth=0)
    if not rows:
        return []
    return rows[:max_records]


def _rows_from(payload: Any, depth: int) -> list[dict]:
    if depth > MAX_DEPTH:
        return []
    if isinstance(payload, list):
        # An array of objects is the record list we want. An array of scalars is
        # a column, not rows, and is refused: there is no way to know which field
        # a bare list of strings belongs in.
        if not payload:
            return []
        if all(isinstance(x, dict) for x in payload):
            return [x for x in payload if isinstance(x, dict)]
        return []
    if isinstance(payload, dict):
        # An error envelope is the single most common JSON response from a real
        # API, and treating `{"error": "not found"}` as a record produces a
        # dataset row containing the word "not found".
        if payload and set(payload) <= _ENVELOPE_KEYS:
            return []
        if not payload:
            return []
        # A wrapper: {"results": [...]} / {"data": [...]} / {"items": [...]}
        for key in ("results", "data", "items", "records", "rows", "list",
                    "entries", "hits"):
            inner = payload.get(key)
            if isinstance(inner, list) and inner:
                return _rows_from(inner, depth + 1)
            if isinstance(inner, dict) and inner:
                return _rows_from(inner, depth + 1)
        return [payload]
    return []


def _key_matches(field_name: str, key: str) -> bool:
    """Does this JSON key answer this requested field?

    Three levels, in decreasing confidence:

    1. normalised equality — `company_name` vs `companyName` vs `company name`;
    2. the alias table — `revenue` answers `turnover`, `sales`, `arr`;
    3. a whole-token alias inside a compound key — `hq_city` answers `location`
       because `city` is an alias and appears as a token, not a substring.

    Substring matching on the whole key is deliberately *not* used: `revenue`
    matching `revenue_forecast` fills a field with a projection, which is a
    different number from the one asked for. Token matching does not have that
    failure — `forecast` is a separate token, so `revenue_forecast` has one
    alias token and one unknown token, and level 3 requires the *majority* of
    tokens to be known before accepting.
    """
    f = _norm(field_name)
    k = _norm(key)
    if not f or not k:
        return False

    fc = _compact(f)
    kc = _compact(k)
    if fc == kc:
        return True

    aliases = _ALIASES.get(fc, ())
    if not aliases:
        # Not a field the alias table knows; fall back to equality only.
        return k == f or kc == fc

    alias_set = {_compact(a) for a in aliases} | {fc}
    if kc in alias_set:
        return True

    # Level 3: token-wise. `hq_city` splits to {hq, city}; both are aliases.
    tokens = [t for t in re.split(r"[^a-z0-9]+", k.lower()) if t]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in alias_set)
    # All tokens must be recognised. A single unknown token usually means the
    # key carries an extra qualifier (`revenue_forecast`, `revenue_usd`) that
    # changes the meaning, and guessing is exactly what this rung exists to
    # avoid — the model rung can still be asked.
    return hits == len(tokens)


def _leaf(value: Any, depth: int = 0) -> tuple[Any, str] | None:
    """(value, serialised form) for something worth storing, or None.

    Numbers and short strings are values. Objects and arrays are not: a cell
    holding `{"a":1,"b":2}` is not an answer to any question the plan asks, and
    rendering it into a quote produces a wall of text that matches nothing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value, json.dumps(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 400:
            return None
        return text, json.dumps(text)
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        # A short list of scalars is a legitimate multi-value cell (a founder
        # list, a list of tags). A long one is not.
        if not value or len(value) > 20:
            return None
        if all(isinstance(v, (str, int, float)) and not isinstance(v, bool)
               for v in value):
            joined = ", ".join(str(v).strip() for v in value)
            return joined, json.dumps(value)
        return None
    return None


def rung(fields: list[dict], ctx: dict) -> dict[str, dict]:
    """The ladder rung: fill `fields` from the page's JSON body.

    `ctx` needs `json_text` — the raw body, un-reduced. The reducer's markdown
    rendering would mangle it, and the quote must resolve against the document as
    stored, which is why the caller passes the original.
    """
    text = ctx.get("json_text")
    if not looks_like_json(text):
        return {}

    rows = parse_records(text)
    if not rows:
        return {}

    # A single-record payload fills one row. A record list fills the first row
    # only, because the ladder's contract is "fill this record", and the
    # multi-record case is handled by the caller's chunking, not here.
    row = rows[0]

    out: dict[str, dict] = {}
    for spec in fields or []:
        name = str(spec.get("name") or "")
        if not name:
            continue
        for key, value in row.items():
            if not _key_matches(name, str(key)):
                continue
            leaf = _leaf(value)
            if leaf is None:
                continue
            stored, raw = leaf
            out[name] = {
                # Flat, matching every other rung's contract
                # (`labelled`, `jsonld`, `selectors` all return
                # `{value, quote, read_method}`). Nesting the whole cell dict
                # under `value` made `_ladder_cells` treat the dict as the
                # value, so the cell stored `{"value": {...}}` and the validator
                # read it as an unverified object — the rung filled correctly and
                # the record was still discarded.
                "value": stored,
                # The quote is the value exactly as it appears in the payload, so
                # `locate_quote` resolves it against real document text and the
                # verification gate treats this like any other rung.
                "quote": raw,
                # `api` is the existing prior for "a structured feed states the
                # field; nothing is inferred". Reusing it rather than inventing a
                # `json` prior keeps `confidence.METHOD_PRIOR` the single source
                # of truth, and this rung is exactly that claim: the payload said it.
                "read_method": "api",
            }
            break

    if out:
        ctx["_json_records"] = len(rows)
    return out


def describe(text: str) -> dict[str, Any]:
    """What this payload looks like, for run reporting.

    Lets a run say "3 pages were JSON; 214 records were available; the plan asked
    for 12 columns of which 5 exist in the payload" instead of reporting a
    zero-yield page with no explanation.
    """
    rows = parse_records(text)
    if not rows:
        return {"is_json": looks_like_json(text), "records": 0}
    keys = sorted({str(k) for k in rows[0]})
    return {"is_json": True, "records": len(rows), "keys": keys,
            "refused_oversize": bool(isinstance(text, str) and len(text) > MAX_BYTES)}