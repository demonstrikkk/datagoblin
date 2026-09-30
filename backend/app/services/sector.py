"""What kind of dataset is this, and how sure are we?

A dataset about listed companies wants different things to be visible than a
dataset about clinical trials or one about schools: the fields that matter, how
to group them, which comparisons are meaningful. This module decides which of
those it is, and — just as important — declines to decide when the evidence is
thin.

Three rules, and they are the project's own rather than this module's:

1. **No LLM.** The dashboard aggregate is cached for 300s and measured at 9ms
   warm; the real judgments left on this system run on free providers that
   rate-limit. Adding a round trip per dataset, per cold load, to a page that
   must always render, buys a label that the schema already states.

2. **Return the evidence, not just the answer.** `sector()` reports which fields
   and descriptions matched and at what confidence. A classification with no
   receipt is a fabrication with a confidence score attached; the UI can show
   "read as markets from ticker, pe_ratio, market_cap" or nothing at all.

3. **`None` is a valid answer.** Below the confidence floor the result is
   `None` and the UI renders its sector-agnostic view. The field profiler
   already refuses to chart a field with one distinct value for exactly this
   reason, and a wrong sector is worse than no sector: it selects the panels,
   and the wrong panels look authoritative.

The signal, strongest first:

- **Schema names and descriptions.** A `FieldSpec` carries `name`, `type` and
  `description`, and descriptions are far richer than names — "Stock ticker
  symbol", "Price to Earnings ratio", "Clinical trial phase". This is also
  what `/api/datasets/{did}` already returns, so nothing new is read from the
  database.
- **Value shape, from the field profiler.** A field the profiler reports as
  `categorical` with 123 distinct values of 1-5 uppercase characters behaves
  nothing like a `company_name`. The profiler's `kind`, `distinct` and
  `shape` are free once `/profile` has been called and settle ties the schema
  cannot.
- **The goal text**, weakest. It is `datasets.name`, truncated to 120
  characters, and it is the only free text available — so it is used only to
  break a tie the schema left open, never to carry a match on its own.
"""

from __future__ import annotations

import re

#: A sector is reported only at or above this score. Calibrated against the real
#: stored datasets: a finance schema scores well past 2, a generic corporate one
#: well under it, and the ambiguous middle is exactly where a wrong guess
#: becomes a confident-looking dashboard.
CONFIDENCE_FLOOR = 2.0

#: A `ticker` field is a stronger signal than a `name` field, so weights are not
#: uniform. A field whose name matches contributes its weight; a field whose
#: *description* matches contributes half, because descriptions are free text
#: the model wrote and can mention a neighbouring domain in passing.
_W_NAME = 1.0
_W_DESC = 0.5

#: Fields whose presence in a schema is itself a domain statement, regardless of
#: anything else. Kept short and specific on purpose: a list of thirty
#: broad terms would classify everything as something.
#
#: `sector` is deliberately absent. "sector" is a finance word but also appears
#: as "sector" in energy, healthcare and public policy, and a dataset with a
#: field literally named `sector` tells you far less than one with `ticker`.
_STRONG_FIELD = {
    "stocks": {"ticker", "exchange", "market_cap", "pe_ratio", "stock_symbol",
               "share_price", "market_capitalisation", "market_capitalization",
               "dividend_yield", "52_week_high", "52_week_low", "beta"},
    "healthcare": {"trial_phase", "clinical_phase", "nct_id", "indication",
                   "therapeutic_area", "sponsor", "phase_completion"},
    "education": {"institution", "enrolment", "enrollment", "student_count",
                  "accreditation", "tuition_fee"},
    "realestate": {"postcode", "zip_code", "floor_area", "bedrooms", "property_type",
                   "listing_price", "sqft"},
    "energy": {"capacity_mw", "generation_mwh", "fuel_type", "commissioned_year",
               "operator"},
    "hr": {"job_title", "seniority", "headcount", "employment_type"},
}

#: Vocabulary for scoring, as (sector, tokens). Token forms are matched against
#: the field name with separators removed, and against the description
#: word-by-word — so `pe_ratio` matches `pe` and `ratio`, and "price to earnings"
#: matches `earnings`.
_VOCAB: dict[str, tuple[str, ...]] = {
    "stocks": ("ticker", "exchange", "marketcap", "peratio", "pe", "dividend",
               "valuation", "sharesoutstanding", "closingprice", "openingprice",
               "52week", "earnings", "float", "ipo", "nse", "bse", "nasdaq",
               "nyse", "equity", "stock", "price", "eps"),
    "healthcare": ("trial", "phase", "nct", "indication", "therapeutic", "sponsor",
                   "clinical", "biotech", "pharma", "therapeuticarea", "medical",
                   "diagnosis", "patient", "dosage", "regulatory"),
    "education": ("institution", "school", "university", "college", "enrolment",
                  "enrollment", "student", "undergraduate", "postgraduate",
                  "accreditation", "campus", "faculty", "curriculum"),
    "realestate": ("property", "listing", "postcode", "realestate", "bedrooms",
                   "bathrooms", "floorarea", "sqft", "tenure", "leasehold",
                   "freehold", "realtor", "mansion", "apartment"),
    "energy": ("capacity", "generation", "turbine", "solar", "windfarm", "gigawatt",
               "megawatt", "fuel", "emissions", "grid", "refinery", "pipeline"),
    "hr": ("headcount", "jobtitle", "seniority", "employmenttype", "recruiter",
           "vacancy", "hiring", "payroll", "attrition", "onboarding"),
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

#: Prefixes that describe the *kind of value* rather than its subject. "company
#: name" is not a finance signal, but "market cap" is, and a substring match on
#: `cap` would fire on both.
_NOISE = {"id", "name", "names", "type", "status", "date", "url", "link",
          "description", "notes", "count", "total", "value", "source"}


def _norm(text: str) -> str:
    return "".join(_TOKEN_SPLIT.split(str(text or "").lower()))


def _words(text: str) -> set[str]:
    return {w for w in _TOKEN_SPLIT.split(str(text or "").lower()) if len(w) > 1}


def _field_name(field: dict) -> str:
    return str(field.get("name") or field.get("field") or "")


def _field_text(field: dict) -> tuple[str, set[str], set[str]]:
    """(squashed name, name tokens, description words) for one schema field."""
    name = _field_name(field)
    desc = str(field.get("description") or "")
    return _norm(name), _words(name), _words(desc)


def _score_field(sector: str, field: dict) -> tuple[float, str]:
    """How strongly one field argues for one sector, and the token that did it."""
    squashed, name_words, desc_words = _field_text(field)
    norm_name = _field_name(field).lower()
    vocab = _VOCAB.get(sector, ())

    # A field the domain *defines* outranks any token match.
    if norm_name in _STRONG_FIELD.get(sector, ()):  # exact
        return _W_NAME, norm_name
    squashed_strong = {_norm(s) for s in _STRONG_FIELD.get(sector, ())}
    if squashed in squashed_strong:
        return _W_NAME, squashed

    best, token = 0.0, ""
    for t in vocab:
        if t in _NOISE:
            continue
        if t == squashed or (len(t) > 3 and t in squashed):
            # A whole-token hit on the name. `pe` is excluded by length, which is
            # why `pe_ratio` relies on `peratio` and `earnings` too.
            if t not in _NOISE and t != "pe":
                if _W_NAME > best:
                    best, token = _W_NAME, t
        if t in name_words and t not in _NOISE:
            if _W_NAME > best:
                best, token = _W_NAME, t
        if t in desc_words and t not in _NOISE:
            if _W_DESC > best:
                best, token = _W_DESC, f"{t} (description)"
    return best, token


def _shape_bonus(sector: str, field: dict, prof: dict | None) -> tuple[float, str]:
    """Adjudicate with what the values look like.

    The profiler's own signals settle ties the schema cannot. A field named
    `symbol` is ambiguous on its own — it is a ticker in one dataset and a
    chemical symbol in another — but a `categorical` field of 120 distinct
    one-to-five character values is a ticker and nothing else.

    This is a tiebreaker, not a classifier: it is capped low so that no amount of
    shape can carry a sector on its own without the schema agreeing.
    """
    if not prof:
        return 0.0, ""
    by_name = {str(f.get("field")): f for f in (prof.get("fields") or [])}
    pf = by_name.get(_field_name(field))
    if not pf:
        return 0.0, ""
    kind = str(pf.get("kind") or "")
    distinct = int(pf.get("distinct") or 0)
    if sector == "stocks" and kind == "categorical" and distinct >= 20:
        # Many short distinct values: a ticker column.
        sample = [str(v.get("value") or "") for v in
                  ((pf.get("shape") or {}).get("values") or [])[:20]]
        if sample and all(1 <= len(s) <= 6 for s in sample):
            return 0.5, f"{distinct} short distinct values"
    if sector == "healthcare" and kind == "categorical" and distinct >= 2:
        vals = {str(v.get("value") or "").lower()
                for v in ((pf.get("shape") or {}).get("values") or [])}
        if vals & {"phase i", "phase ii", "phase iii", "phase iv", "phase 1",
                   "phase 2", "phase 3", "phase 4", "i", "ii", "iii", "iv"}:
            return 0.5, "trial phase values"
    return 0.0, ""


def _goal_tiebreak(goal: str, candidates: list[str]) -> str | None:
    """Use the goal text only to break a tie the schema left open."""
    words = _words(goal)
    if not words:
        return None
    scored = []
    for s in candidates:
        hit = len(words & set(_VOCAB.get(s, ())))
        if hit:
            scored.append((hit, s))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    # A single incidental word is not a tiebreak.
    return scored[0][1] if scored[0][0] >= 2 else None


def shape_summary(records: list[dict], schema: list[dict],
                  max_values: int = 12) -> dict:
    """The smallest useful shape summary for the tiebreaker.

    Deliberately not `profile.profile()`: that reads a dataset through the
    repository, computes trust, place panels and timelines, and is the right
    thing for a dataset page. The classifier only needs to know whether a field
    is categorical and how many short distinct values it has, so this works on
    records already in hand and costs a dict per field.

    Kept here rather than in the caller so the two routes into `sector()` cannot
    disagree about what a shape means.
    """
    acc: dict[str, dict] = {}
    for f in schema or []:
        name = _field_name(f)
        if name:
            acc[name] = {"counts": {}}
    for r in records or []:
        for name, cell in (r.get("fields") or {}).items():
            slot = acc.get(name)
            if slot is None:
                continue
            raw = cell.get("value") if isinstance(cell, dict) else cell
            if raw is None or raw == "":
                continue
            k = str(raw).strip()
            if k:
                slot["counts"][k] = slot["counts"].get(k, 0) + 1

    out: list[dict] = []
    for name, slot in acc.items():
        counts = slot["counts"]
        distinct = len(counts)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:max_values]
        out.append({
            "field": name,
            "kind": "categorical" if distinct >= 2 else ("scalar" if distinct else "empty"),
            "distinct": distinct,
            "shape": {"values": [{"value": k, "n": n} for k, n in top]},
        })
    return {"fields": out}


def sector_for_records(schema: list[dict] | None, records: list[dict] | None,
                       goal: str = "", floor: float = CONFIDENCE_FLOOR) -> dict:
    """`sector()` for a caller that already has the records in hand.

    The dashboard aggregate reads every dataset's records anyway, so it uses this
    rather than calling the profiler per dataset — one dict per field instead of
    a second pass over the store, inside an aggregate measured at 9ms warm.
    """
    return sector(schema, profile=shape_summary(records or [], schema or []),
                  goal=goal, floor=floor)


def sector(schema: list[dict] | None, profile: dict | None = None,
           goal: str = "", floor: float = CONFIDENCE_FLOOR) -> dict:
    """Classify a dataset. Always returns a dict; `sector` is None when unsure.

    The return is a receipt, not a label:

        sector          "stocks" | None
        confidence      score, 0.0 when None
        method          "schema" | "schema+shape" | "none"
        matched_fields  [{"field": ..., "signal": ..., "score": ...}]
        considered      every sector that scored at all, best first
        floor           the threshold that was applied
    """
    schema = schema or []
    if not schema:
        return {"sector": None, "confidence": 0.0, "method": "none",
                "matched_fields": [], "considered": [], "floor": floor}

    scores: dict[str, float] = {k: 0.0 for k in _VOCAB}
    matched: dict[str, list[dict]] = {k: [] for k in _VOCAB}

    for field in schema:
        for s in _VOCAB:
            pts, token = _score_field(s, field)
            if pts <= 0:
                continue
            scores[s] += pts
            matched[s].append({"field": _field_name(field), "signal": token,
                               "score": round(pts, 2)})
            bonus, why = _shape_bonus(s, field, profile)
            if bonus > 0:
                scores[s] += bonus
                matched[s].append({"field": _field_name(field), "signal": why,
                                   "score": round(bonus, 2)})

    considered = sorted(
        ({"sector": s, "score": round(v, 2)} for s, v in scores.items() if v > 0),
        key=lambda r: (-r["score"], r["sector"]))

    if not considered:
        return {"sector": None, "confidence": 0.0, "method": "none",
                "matched_fields": [], "considered": [], "floor": floor}

    best = considered[0]
    # A tie, or a near-tie, is not a classification.
    if len(considered) > 1 and best["score"] - considered[1]["score"] < 0.75:
        tiebreak = _goal_tiebreak(goal, [c["sector"] for c in considered[:2]])
        if tiebreak is None:
            return {"sector": None, "confidence": round(best["score"], 2),
                    "method": "ambiguous", "matched_fields": matched[best["sector"]],
                    "considered": considered, "floor": floor}
        best = next(c for c in considered if c["sector"] == tiebreak)

    if best["score"] < floor:
        return {"sector": None, "confidence": round(best["score"], 2),
                "method": "below-floor", "matched_fields": matched[best["sector"]],
                "considered": considered, "floor": floor}

    method = "schema+shape" if profile else "schema"
    return {"sector": best["sector"], "confidence": round(best["score"], 2),
            "method": method,
            "matched_fields": sorted(matched[best["sector"]],
                                     key=lambda m: -m["score"]),
            "considered": considered, "floor": floor}
