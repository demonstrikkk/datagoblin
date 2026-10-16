"""Entity-conformance gate: drop values that are not what the field asks for.

THE HOLE THIS CLOSES

Every stage of the pipeline verified *quotation* and none checked *entity
conformance*. An article headline in `company_name` appears verbatim in its own
quote, so validation passes it, the judge confirms the quote supports it (it
does), and dedupe keeps it (it is distinct). The evidence chain works exactly
as designed — the design just never asks "is this value an instance of what
the user requested". A dataset of "10 London AI startups" came back with news
headlines as companies, and every signal on the page called it verified.

WHAT IT DOES

After validation, before dedupe: for fields that name entities (companies,
people, and the like), drop values that are observably not entities —
headlines, questions, URLs, sentences — and record every drop with its reason.
A record whose primary entity field ends up empty is dropped as a whole. The
drop log is returned alongside the kept records so the run view can state
"kept 3, dropped 5" instead of silently publishing 8 rows of mixed truth.

WHAT IT DOES NOT DO (STATED, NOT HIDDEN)

  - Wrong-but-plausible attribution ("Yann LeCun" as a founder of the wrong
    company) is beyond heuristics. That is a cross-field consistency judgement
    for the model layer, not a shape rule.
  - Off-topic-but-wellformed records ("The Boring Company" for a London query)
    stay. Topic relevance is a retrieval problem owned by discovery, not by a
    shape gate that cannot see the web.
  - Non-entity fields (locations, amounts, dates, descriptions) are untouched.
    A street address as a location is a granularity question, not a
    non-entity, and dropping real data for being precise would be worse.

All rules are deterministic string checks. No model, no network, no budget.

Matching is token-based, not substring-based. The previous rules were plain
`in` tests over the lowercased field name and the value, and both were wrong:

* the field hint `" name"` (with a leading space) can never match `company_name`,
  because a snake_case field is joined by an underscore, not a space. The hint
  was dead for the naming convention the planner actually produces;
* the value markers `" beat "`, `" record "`, `" ipo "` and `" vs "` matched
  inside any word containing them, so "Heartbeat Labs", "Beat Foods",
  "Record & Roll" and "IPO Partners" — all real company names — were dropped with
  the reason "value carries headline content markers". The module docstring
  claimed ordinary names carry none of these markers, which was false.

A name is rejected for containing a *whole word* that only makes sense in
headline prose, or for being long enough to be a sentence. Word-boundary
matching removes the substring problem without loosening either rule.
"""
from __future__ import annotations

import re

#: Fields whose values must denote entities. Matched against the field name split
#: into tokens on `_`, `-`, spaces and case boundaries, so `company_name`,
#: `founderNames` and `Author` all resolve to a `company`/`founder`/`author` token
#: rather than relying on a substring that happens to line up.
_ENTITY_TOKENS = frozenset({
    "company", "companies", "founder", "founders", "startup", "startups",
    "person", "people", "author", "authors", "ceo", "cfo", "cto", "coo",
    "executive", "executives", "team", "member", "members",
    "organisation", "organization", "organisations", "organizations",
    "org", "employer", "employers", "name", "names", "vendor", "supplier",
    "customer", "client", "publisher", "owner", "operator", "agency",
    "investor", "investors", "employee", "employees", "candidate",
})

#: Substrings that survive tokenising because they are not words on their own.
_ENTITY_SUBSTRINGS = ("compan",)  # `company`, `companies`, `companionship`

#: Headline prefixes: present in news titles, never in names.
_HEADLINE_PREFIXES = ("exclusive:", "breaking:", "watch:", "list:",
                      "opinion:", "analysis:", "sponsored:")

#: Headline content markers, matched as whole words.
#:
#: These are phrases that describe *news* rather than *things*. "Raises" and
#: "Secures" are what a funding headline says and a company name does not; they
#: are absent from the previous list, which is why that list had to fall back on
#: substrings like " beat " and got company names wrong.
#:
#: `vs`/`versus` is deliberately NOT here. "Nike versus Adidas" is a comparison
#: page title, but "Vs" appears inside real names, and a comparison phrase is not
#: evidence of a headline on its own.
_HEADLINE_MARKERS = (
    "funding round", "funding deal", "funding record", "series a", "series b",
    "series c", "series d", "seed round",
    "biggest", "largest", "fastest growing", "fastest-growing",
    "raises", "raised", "secures", "lands", "bags", "snags",
    "top 10", "top ten", "best companies", "top companies",
    "shuts down", "files for bankruptcy", "is acquired", "to be acquired",
    "announces", "announcement", "exclusive", "breaking",
)

#: Whole-word forms of the markers above, with an optional plural suffix.
#:
#: The plural matters: "funding record" alone does not match "Funding Records"
#: under `\b`, because `s` is a word character, so a real headline — one of the
#: exact shapes this gate exists to catch — passed straight through. The earlier
#: substring rules matched plurals by accident; these must do it on purpose.
_HEADLINE_MARK_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _HEADLINE_MARKERS) + r")(?:es|s)?\b",
    re.IGNORECASE,
)

_TOP_N_PATTERN = re.compile(r"\btop\s*\d+\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)

#: Above this many words, a company/person value is a sentence. The longest
#: legitimate names run to ~6 words; headlines start at ~9.
_HEADLINE_WORDS = 8

#: Splits `company_name`, `companyName` and `Company Name` alike.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FIELD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _field_tokens(field_name: str) -> set[str]:
    spaced = _CAMEL_RE.sub(" ", str(field_name or ""))
    return {t.lower() for t in _FIELD_SPLIT_RE.split(spaced) if t}


def is_entity_field(field_name: str) -> bool:
    """Does this field hold entity names?

    Token-based, so `company_name` matches on `company` and `name` rather than
    on a substring with a hardcoded separator that only lines up for some
    spellings and not others.
    """
    tokens = _field_tokens(field_name)
    if not tokens:
        return False
    if tokens & _ENTITY_TOKENS:
        return True
    return any(any(h in t for h in _ENTITY_SUBSTRINGS) for t in tokens)


def _cell_text(cell) -> str:
    if isinstance(cell, dict):
        v = cell.get("value")
    else:
        v = cell
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def reject_entity_value(value: str) -> str | None:
    """Why this value is not an entity name, or None if it passes.

    Returns a short machine-usable reason. Every branch names the marker that
    fired, so the drop log reads as an audit rather than a verdict.
    """
    text = str(value or "").strip()
    if not text:
        return None
    low = text.lower()
    words = text.split()

    if _URL_PATTERN.search(text):
        return "value is a URL, not a name"
    if "?" in text:
        return "value asks a question (headline)"
    head = low[:12]
    if any(head.startswith(p) for p in _HEADLINE_PREFIXES):
        return "value carries a headline prefix"
    if _HEADLINE_MARK_RE.search(text):
        return "value carries headline content markers"
    if _TOP_N_PATTERN.search(text):
        return "value is a ranked-list title"
    if len(words) > _HEADLINE_WORDS:
        return f"value is {len(words)} words (headline length)"
    return None


def apply(records: list[dict], schema: list) -> tuple[list[dict], list[dict]]:
    """Gate validated records. Returns (kept, drops).

    A drop is `{record_index, field, value, reason}`; a record-level drop has
    `field: None` and names the empty primary field. The primary entity field
    is the first entity field in schema order — the column the dataset is
    "about" — falling back to the first schema field, falling back to no
    record-level drops at all when nothing can be identified.
    """
    fields = [f.get("name") for f in (schema or [])
              if isinstance(f, dict) and f.get("name")]
    entity_fields = [f for f in fields if is_entity_field(f)]
    primary = (entity_fields or fields or [None])[0]

    kept: list[dict] = []
    drops: list[dict] = []
    for i, rec in enumerate(records or []):
        rec_fields = rec.get("fields") if isinstance(rec, dict) else None
        if not isinstance(rec_fields, dict):
            kept.append(rec)
            continue
        for name in entity_fields:
            if name not in rec_fields:
                continue
            text = _cell_text(rec_fields[name])
            if not text:
                continue
            reason = reject_entity_value(text)
            if reason:
                drops.append({"record_index": i, "field": name,
                              "value": text[:160], "reason": reason})
                # Empty the value but keep the cell shape: downstream stages
                # read `cell.get("value")`, and a bare None in its place is an
                # AttributeError waiting for dedupe. `rejected` is the honest
                # status — failed validation, dropped rather than softened.
                cell = rec_fields[name]
                if isinstance(cell, dict):
                    cell["value"] = None
                    cell["verification_status"] = "rejected"
                else:
                    rec_fields[name] = None
        if primary and _cell_text(rec_fields.get(primary)) == "":
            drops.append({"record_index": i, "field": None,
                          "value": "",
                          "reason": f"primary entity field '{primary}' holds "
                                    f"no surviving value"})
            continue
        kept.append(rec)
    return kept, drops
