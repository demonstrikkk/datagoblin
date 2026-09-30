"""What kind of dataset is this, and how sure are we?

A dataset about venture-backed companies wants different things visible than one
about listed securities, and both want different things than a directory of
non-profits. This module decides which it is and — just as importantly — declines
when the evidence is thin.

Three rules, and they are the project's own rather than this module's:

1. **No LLM.** The dashboard aggregate is cached and must always render; the real
   judgements left on this system run on free providers that rate-limit. A round
   trip per dataset to produce a label the schema already states is the wrong
   trade. A deterministic scorer is also *checkable*, which a classifier that
   costs money is not.

2. **Return the evidence, not just the answer.** `sector()` reports which fields
   matched, at what weight, and for what reason. A classification with no receipt
   is a fabrication with a confidence score attached.

3. **`None` is a valid answer.** Below the floor, or on a tie, the result is
   `None` and the UI renders its sector-agnostic view. The field profiler already
   refuses to chart a field with one distinct value for the same reason, and a
   wrong sector is worse than none: it selects the panels, and the wrong panels
   look authoritative.

## The vocabulary is derived, not invented

The first version of this file was written from imagination and matched almost
nothing: the live corpus — 20 datasets, 116 distinct field names, frozen at
`tests/fixtures/schema_corpus.json` — returned `{"sector": null, "method":
"none"}` for a dataset about AI startups. Every token in it belonged to a domain
this workspace had never contained.

So the vocabulary below is read off the corpus, and `test_sector.py` runs this
scorer over all 20 real schemas rather than over invented examples. When the
planner invents a new field name, that test is what notices.

## Three weights, because a ticker is not a stock table

`_STRONG` is a whole field name that is a domain statement on its own.
`_VOCAB` tokens score in the field name, and again at half weight when they only
appear in the free-text description. `_WEAK` scores at half of both.

The weak tier exists for `ticker_symbol` and `is_public`. A collaboration-software
vendor dataset has both, and reading it as a stock table because it carries a
ticker column would be wrong — the subject is vendors, some of which happen to be
listed. It scores 1.0 on markets against 1.5 for venture and 1.5 for workforce,
which is a tie, so it is refused. That refusal is the correct answer.

`valuation`, `fiscal_year` and `revenue` are likewise not finance signals: a
"companies with their valuations" dataset is a corporate lookup, and a
"publish their annual revenue" dataset is filings, not a market.
"""

from __future__ import annotations

import re

#: A sector is reported only at or above this score.
#:
#: Calibrated against the frozen corpus. The decisive datasets score well past 3
#: (Indian stocks ~11, YC companies ~17, Delhi NGOs ~9); the deliberately
#: ambiguous ones score under it (EU revenue filings ~1.5, collaboration
#: vendors tie at 1.5). The floor sits in the gap.
CONFIDENCE_FLOOR = 2.0

#: Two sectors closer than this are a tie, not a classification. Wide enough
#: that a single extra field flips a domain, narrow enough that three do not.
TIE_MARGIN = 0.75

_W_NAME = 1.0
_W_DESC = 0.5

#: Half weight, for a token that says something is *possible* rather than
#: something that *is*.
#:
#: `ticker_symbol` and `is_public` both appear on a collaboration-software
#: vendor dataset whose subject is vendors, most of which happen not to be
#: listed. At full weight those two fields alone carried that dataset to 2.0 and
#: it came out as `markets` — confidently, and wrong. Halved, they score 1.0,
#: the dataset ties `venture` and `workforce` at 1.5, and it is refused, which is
#: the honest answer.
_W_WEAK = 0.5

#: Field names that are a domain statement by themselves.
_STRONG: dict[str, frozenset[str]] = {
    "venture": frozenset({
        "yc_slug", "yc_profile_url", "batch_season", "batch_number",
        "batch_type", "funding_round", "funding_stage", "funding_amount",
        "total_funding_raised", "total_funding_usd", "latest_round_type",
        "latest_round_date", "latest_funding_round_type", "latest_funding_amount",
        "latest_funding_date", "latest_funding_investors", "valuation_usd",
        "founder_names", "founder_count", "founder_titles", "founder_links",
    }),
    "markets": frozenset({
        "market_cap", "pe_ratio", "isin", "listing_date", "last_price",
        "change_percent", "exchange",
    }),
    "nonprofit": frozenset({
        "ngo_name", "organization_name", "registration_id",
        "registration_number", "legal_status", "beneficiaries_reached",
    }),
    "workforce": frozenset({
        "is_hiring", "open_roles", "open_roles_count", "open_engineering_roles",
        "open_engineering_roles_count", "active_engineering_roles",
        "team_size", "employee_count", "staff_count",
    }),
    # Attested by no dataset in the corpus. Kept because an unproven entry costs
    # nothing — it only ever appears in `considered` — and a real estate or
    # clinical dataset should not need the vocabulary rewritten first. Unproven
    # is not the same as wrong: none of these can fire on the current data, so
    # none of them can be wrong on it either.
    "realestate": frozenset({
        "listing_price", "floor_area", "bedrooms", "bathrooms", "postcode",
        "property_type", "tenure",
    }),
    "healthcare": frozenset({
        "trial_phase", "clinical_phase", "nct_id", "indication",
        "therapeutic_area",
    }),
    "education": frozenset({
        "institution", "accreditation", "enrolment", "enrollment",
        "student_count", "campus",
    }),
    "energy": frozenset({
        "capacity_mw", "generation_mwh", "fuel_type", "operator",
    }),
}

#: Tokens matched against the squashed field name and the description words.
_VOCAB: dict[str, tuple[str, ...]] = {
    "venture": (
        "founder", "funding", "funded", "investor", "investment", "raise",
        "raised", "round", "series", "seed", "batch", "combinator",
        "valuation", "capraise", "accelerator", "term", "terms", "one_liner",
    ),
    "markets": (
        "ticker", "symbol", "exchange", "marketcap", "peratio", "earnings",
        "eps", "dividend", "isin", "listing", "listed", "float", "ipo",
        "nse", "bse", "nasdaq", "nyse", "equity", "stock", "price", "volume",
        "market",
    ),
    "nonprofit": (
        "ngo", "organization", "organisation", "charity", "charitable",
        "foundation", "trust", "society", "darpan", "fcra", "registration",
        "beneficiar", "beneficiary", "budget", "grant", "csr", "donor",
        "volunteer", "operational",
    ),
    "workforce": (
        "hiring", "hire", "vacanc", "openroles", "roles", "role", "headcount",
        "employee", "staff", "team", "career", "recruit", "payroll",
        "attrition", "position",
    ),
    "realestate": (
        "property", "realestate", "listing", "postcode", "bedroom", "bathroom",
        "floorarea", "sqft", "tenure", "leasehold", "freehold", "realtor",
        "apartment", "mansion",
    ),
    "healthcare": (
        "trial", "phase", "nct", "indication", "therapeutic", "sponsor",
        "clinical", "biotech", "pharma", "medical", "diagnosis", "patient",
        "dosage", "regulatory",
    ),
    "education": (
        "institution", "school", "university", "college", "enrolment",
        "enrollment", "student", "undergraduate", "postgraduate",
        "accreditation", "campus", "faculty", "curriculum",
    ),
    "energy": (
        "capacity", "generation", "turbine", "solar", "windfarm", "gigawatt",
        "megawatt", "fuel", "emissions", "grid", "refinery", "pipeline",
    ),
}

#: Tokens that argue against a domain as well as for one.
#:
#: `listing` is the interesting one: a listed security is `listing`, a property
#: for sale is a `listing`, and the same word appears in both. `volume` likewise
#: is trading volume in one domain and shipping volume in another. Tokens here
#: are simply never counted, which is why neither domain can claim a field on
#: the strength of a word they share.
_SHARED = frozenset({
    "listing", "volume", "public", "private", "market", "price", "value",
    "rating", "score", "title", "role", "phase", "level", "type", "status",
    "round", "term",
})

#: Field names that say something is *possible* rather than something that *is*.
#:
#: Keyed on the whole name rather than on a token, because the token is the same
#: in both cases and the difference is the column. A `market_cap` column means
#: this is a market table. A `ticker_symbol` column means some of the vendors in
#: this directory are listed — true of every company dataset that happens to
#: include publicly traded ones, and not a reason to call the directory a stock
#: table. At full weight those four fields carried the collaboration-vendor
#: datasets to 2.0 and they came out as `markets`: confidently, and wrong.
#:
#: Halved, they score 1.0. Those datasets then tie `venture` and `workforce` at
#: 1.5 and are refused, which is the honest answer — the subject is vendors, and
#: no single domain describes it.
_WEAK_FIELDS: frozenset[str] = frozenset({
    "ticker_symbol", "ticker_or_registration_id",
    "is_public", "is_public_company",
})

#: Tokens that describe the *shape* of a value rather than its subject.
#:
#: "company name" is not a venture signal, but "market cap" is, and a substring
#: match on `cap` would fire on both. `round` is here for the same reason:
#: `funding_round` is venture, but so is a basketball round.
_NOISE = frozenset({
    "id", "ids", "name", "names", "url", "urls", "link", "links", "note",
    "notes", "count", "total", "source", "description", "photo", "photos",
    "tag", "tags", "date", "year", "number", "index",
})

#: Tokens too short to survive as a bare match. `pe` matches `pe_ratio` by
#: accident more often than by intent.
_SHORT = frozenset({"pe", "is", "of", "to", "in", "ai", "us", "uk", "eu"})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

#: Coverage of the vocabulary against the frozen corpus, stated as three
#: different things rather than one vague claim.
#:
#: ATTESTED — a real dataset in the workspace is about this.
ATTESTED = ("venture", "markets", "nonprofit")
#: SUPPORTED — the vocabulary fires on real fields in the corpus, but no dataset's
#: *subject* is this domain. `workforce` scores on `open_roles`, `is_hiring` and
#: `team_size` across the startup datasets and the venture reading is always the
#: stronger one, which is correct: a directory of funded companies that also lists
#: who they are hiring is a venture dataset. A hypothetical headcount dataset would
#: classify as workforce; no stored one is.
SUPPORTED = ("workforce",)
#: UNATTESTED — present so a dataset in one of these domains does not need this
#: file rewritten first, and matched by nothing in the corpus. Unproven is not the
#: same as wrong: an entry that never fires cannot be wrong on this data either.
UNATTESTED = ("realestate", "healthcare", "education", "energy")


def _norm(text: str) -> str:
    return "".join(_TOKEN_SPLIT.split(str(text or "").lower()))


def _words(text: str) -> set[str]:
    return {w for w in _TOKEN_SPLIT.split(str(text or "").lower()) if len(w) > 1}


def _field_name(field: dict) -> str:
    return str(field.get("name") or field.get("field") or "")


def _is_strong(sector: str, name: str, squashed: str) -> bool:
    if name in _STRONG.get(sector, frozenset()):
        return True
    return squashed in {_norm(s) for s in _STRONG.get(sector, frozenset())}


def _score_field(sector: str, field: dict) -> tuple[float, str]:
    """How strongly one field argues for one sector, and the tokens that did it.

    Name hits and description hits are counted separately and added, capped at
    one full name hit. Summing rather than taking the best token is what lets a
    field carry the sector twice over when the description confirms the name —
    `symbol` / "Exchange ticker symbol identifying the stock" — without letting
    one chatty description beat four disagreeing field names.

    Descriptions are matched by prefix as well as by equality, because the
    planner writes them about the value rather than about the field:
    "List of founders" has to answer to `founder`, "Approximate number of
    employees" to `employee`, "number of women or girls benefited" to
    `beneficiar`. Without that, three of the corpus's own decisive fields scored
    nothing and their datasets fell under the floor.
    """
    name = _field_name(field).lower()
    squashed = _norm(name)
    desc_words = _words(str(field.get("description") or ""))
    tokens = _VOCAB.get(sector, ())
    scale = _W_WEAK if name in _WEAK_FIELDS else 1.0

    if _is_strong(sector, name, squashed):
        return _W_NAME * scale, f"field name {name}"

    def _usable(t: str) -> bool:
        return t not in _NOISE and t not in _SHARED and t not in _SHORT

    score = 0.0
    why: list[str] = []
    for t in tokens:
        if not _usable(t):
            continue
        # Name: an exact token, a substring for compound field names, or a
        # prefix of a name token. The length floor stops `pe` inside `pe_ratio`
        # counting twice, since `pe` is excluded above and `peratio` covers it.
        if (t == squashed) or (len(t) > 3 and t in squashed) or any(
                w.startswith(t) for w in _words(name) if len(t) >= 4):
            score += _W_NAME * scale
            why.append(t)
            break

    # A description can only *confirm* what the name already suggested. It can
    # never introduce a domain the field name does not mention.
    #
    # Descriptions are prose *about* a value, and they enumerate. The Delhi NGO
    # datasets carry `focus_areas` — "Program themes, including gender equality,
    # women's empowerment, skill training, livelihood, education, health, safety,
    # and legal aid" — and that single line matched `education` and `healthcare`
    # at 0.5 each, on a dataset about charities. Programme themes are the subject
    # of the records, not the kind of dataset they are.
    #
    # Requiring a name hit first makes the rule "the name is the schema, the
    # description is commentary". It costs nothing where it matters:
    # `symbol` / "Exchange ticker symbol identifying the stock" still reaches 1.5,
    # and `funding_stage` / "Latest funding stage such as seed, Series A" still
    # reaches 1.5.
    if score:
        for t in tokens:
            if not _usable(t):
                continue
            if t in desc_words or any(
                    (len(t) >= 4 and w.startswith(t))
                    or w.startswith(t[:max(4, len(t) - 1)]) for w in desc_words):
                score += _W_DESC * scale
                why.append(f"{t} (description)")
                break

    return (min(score, _W_NAME * scale + _W_DESC * scale), ", ".join(why)) if score else (0.0, "")


def _shape_bonus(sector: str, field: dict, prof: dict | None) -> tuple[float, str]:
    """Adjudicate with what the values look like.

    The profiler's own signals settle ties the schema cannot. A field named
    `symbol` is a ticker in one dataset and a chemical symbol in another; a
    `categorical` field of 120 distinct one-to-five character values is a ticker
    and nothing else.

    Capped, so no amount of shape can carry a sector on its own without the schema
    agreeing.
    """
    if not prof:
        return 0.0, ""
    by_name = {str(f.get("field")): f for f in (prof.get("fields") or [])}
    pf = by_name.get(_field_name(field))
    if not pf:
        return 0.0, ""
    kind = str(pf.get("kind") or "")
    distinct = int(pf.get("distinct") or 0)
    values = [str(v.get("value") or "")
              for v in ((pf.get("shape") or {}).get("values") or [])[:20]]
    low = {v.lower() for v in values}

    if sector == "markets" and kind == "categorical" and distinct >= 20:
        if values and all(1 <= len(v) <= 6 and v.isupper() for v in values):
            return 0.5, f"{distinct} short uppercase distinct values"
    if sector == "nonprofit" and kind == "categorical" and distinct >= 3:
        if any(("ngo" in v or "trust" in v or "society" in v
                or "charit" in v or "foundation" in v) for v in low):
            return 0.5, "registration-status values"
    if sector == "healthcare" and kind == "categorical" and distinct >= 2:
        if low & {"phase i", "phase ii", "phase iii", "phase iv", "phase 1",
                  "phase 2", "phase 3", "phase 4", "i", "ii", "iii", "iv"}:
            return 0.5, "trial phase values"
    return 0.0, ""


def _goal_tiebreak(goal: str, candidates: list[str]) -> str | None:
    """Use the goal text only to break a tie the schema left open."""
    words = _words(goal)
    if not words:
        return None
    scored: list[tuple[int, str]] = []
    for s in candidates:
        vocab = {t for t in _VOCAB.get(s, ()) if t not in _NOISE and t not in _SHARED}
        hit = len(words & vocab)
        if hit:
            scored.append((hit, s))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    # A single incidental word is noise, not a tiebreak.
    return scored[0][1] if scored[0][0] >= 2 else None


def shape_summary(records: list[dict], schema: list[dict],
                  max_values: int = 12) -> dict:
    """The smallest useful shape summary for the tiebreaker.

    Deliberately not `profile.profile()`: that reads a dataset through the
    repository and computes trust, place panels and timelines, which is right for
    a dataset page. The classifier only needs to know whether a field is
    categorical and what its values look like, so this works on records already
    in hand and costs one dict per field.

    Kept here so the two routes into `sector()` cannot disagree about what a
    shape means.
    """
    acc: dict[str, dict] = {}
    for f in schema or []:
        name = _field_name(f)
        if name:
            acc[name] = {}
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
                slot[k] = slot.get(k, 0) + 1

    out: list[dict] = []
    for name, counts in acc.items():
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
    rather than calling the profiler per dataset — one dict per field instead of a
    second pass over the store.
    """
    return sector(schema, profile=shape_summary(records or [], schema or []),
                  goal=goal, floor=floor)


def sector(schema: list[dict] | None, profile: dict | None = None,
           goal: str = "", floor: float = CONFIDENCE_FLOOR) -> dict:
    """Classify a dataset. Always returns a dict; `sector` is None when unsure.

    The return is a receipt, not a label:

        sector          "venture" | "markets" | "nonprofit" | "workforce" | None
        confidence      score, 0.0 when None
        method          "schema" | "schema+shape" | "ambiguous" | "below-floor"
                        | "none"
        matched_fields  [{"field": ..., "signal": ..., "score": ...}]
        considered      every sector that scored at all, best first
        floor           the threshold that was applied
        tie_margin      the gap that counts as a tie
    """
    schema = schema or []
    empty = {"sector": None, "confidence": 0.0, "method": "none",
             "matched_fields": [], "considered": [], "floor": floor,
             "tie_margin": TIE_MARGIN}
    if not schema:
        return empty

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
        return empty

    best = considered[0]
    if len(considered) > 1 and best["score"] - considered[1]["score"] < TIE_MARGIN:
        # A tie, or a near-tie, is not a classification.
        tiebreak = _goal_tiebreak(goal, [c["sector"] for c in considered[:2]])
        if tiebreak is None:
            return {"sector": None, "confidence": round(best["score"], 2),
                    "method": "ambiguous",
                    "matched_fields": matched[best["sector"]],
                    "considered": considered, "floor": floor,
                    "tie_margin": TIE_MARGIN}
        best = next(c for c in considered if c["sector"] == tiebreak)

    if best["score"] < floor:
        return {"sector": None, "confidence": round(best["score"], 2),
                "method": "below-floor", "matched_fields": matched[best["sector"]],
                "considered": considered, "floor": floor,
                "tie_margin": TIE_MARGIN}

    return {"sector": best["sector"], "confidence": round(best["score"], 2),
            "method": "schema+shape" if profile else "schema",
            "matched_fields": sorted(matched[best["sector"]],
                                     key=lambda m: -m["score"]),
            "considered": considered, "floor": floor, "tie_margin": TIE_MARGIN}
