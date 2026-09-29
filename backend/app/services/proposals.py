"""Turn a sentence into an executable proposal. Never into a mutation.

The point of asking a question in words is that you should not have to know
which endpoint fills the gap you noticed. But the answer to "add the funding
round" must not be a language model writing into a dataset — that is how a
dataset whose entire claim is *every value carries a quote* stops meaning it.

So the shape is always the same, and it is deliberate:

    sentence -> classified intent -> a concrete proposal the user can read
             -> explicit approval -> the existing verified path executes it

Three intents, because those are the three things actually worth doing to a
dataset. Two more were added once backfill could not express the request:

* ``query``      — answer a question by running the guarded SQL path.
* ``backfill``   — name fields that are empty and can be filled from stored
  pages, and say what it would cost before doing it. ``include_partial`` widens
  it to columns that are filled on some records and not others.
* ``refine``     — change the plan the dataset came from and show a field diff.
* ``add_column`` — declare a field the schema does not have, then backfill it.
  Separate from ``backfill`` because the schema write is a different kind of
  change from a value write, and a user who says "add a funding column" means
  both without having said the second one.
* ``refresh``    — re-read the sources a dataset already used and re-verify the
  values they supplied. Separate because it is the one intent that can replace
  a value that already exists.

There is no sixth. A proposal that cannot be classified is reported as
unrecognised along with the five that exist, rather than being interpreted
loosely. Being told "I did not understand that" is the correct answer far more
often than forcing a sentence into one of these, and it is the only answer that
cannot quietly do the wrong thing.
"""
from __future__ import annotations

import re

#: What the user asked for, and how each is carried out. `run` is the name of
#: the existing, already-verified path each intent delegates to — this module
#: never performs the work itself.
INTENTS = ("query", "backfill", "refine", "add_column", "refresh")

_BACKFILL_HINTS = (
    "add", "fill", "missing", "backfill", "empty", "blank", "capture",
    "extract", "get", "need", "want", "also", "plus",
)
_BACKFILL_TARGETS = (
    "field", "column", "value", "data", "info", "detail", "details", "round",
    "founder", "funding", "valuation", "size", "industry", "city", "country",
    "year", "ticker", "employee", "hq", "url", "website", "salary", "price",
)
_REFRESH_HINTS = (
    "refresh", "recheck", "re-check", "update", "stale", "outdated", "wrong",
    "changed", "revisit", "re-read", "reread", "current", "now", "correct",
    "fix", "repair", "again",
)
_REFRESH_TARGETS = (
    "source", "sources", "page", "pages", "url", "site", "website", "value",
    "values", "cell", "cells", "data",
)
_REFINE_HINTS = (
    "instead", "rather", "change", "narrow", "wider", "more", "fewer",
    "limit", "only", "restrict", "drop", "remove", "raise", "increase",
    "decrease", "reduce", "adjust", "tweak", "swap",
)
_PLAN_SUBJECTS = (
    "run", "search", "source", "page", "depth", "crawl", "budget", "plan",
    "query", "queries", "count", "records", "goal",
)

#: A bare identifier, or an identifier in dots. Used to pull the field names a
#: request is actually about, so the proposal can check they exist.
_IDENT = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\b", re.I)


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _IDENT.finditer(text or "")]


def classify(question: str) -> tuple[str, float]:
    """Which intent, and how strongly. (intent, score).

    Scored rather than pattern-matched in order, because a sentence often
    contains hints for more than one: "add funding but only from the UK" is a
    backfill that also refines the plan. The stronger reading wins and the
    weaker one is reported, so the user can see what was nearly chosen.
    """
    words = set(_tokens(question))
    if not words:
        return "", 0.0

    # A question mark settles it. Asking and requesting are different acts, and
    # a sentence that asks must never be able to write — "is the funding round
    # empty?" names a gap and a change in the same breath. Damping the scores
    # was not enough: the gap words alone already outscore the question. So the
    # punctuation decides, which means a misread sentence fails toward reading
    # rather than toward writing.
    if question.strip().endswith("?"):
        return "query", 0.0

    backfill = len(words & set(_BACKFILL_HINTS)) + 0.5 * len(words & set(_BACKFILL_TARGETS))
    refine = len(words & set(_REFINE_HINTS)) + 0.5 * len(words & set(_PLAN_SUBJECTS))
    refresh = len(words & set(_REFRESH_HINTS)) + 0.5 * len(words & set(_REFRESH_TARGETS))

    # "add a column" and "fill the missing funding" are the same request by
    # another name, and which one it is only matters for the schema write. The
    # distinction that is made here is the *opposite* of the intuitive one: a
    # sentence that names a column as new and does not name an existing gap is
    # a schema change, because backfill alone cannot make a field exist.
    add_column = 0.0
    if _asks_for_new_column(question or ""):
        add_column = max(backfill, 1.5) + 1.0

    scores = {"backfill": backfill, "refine": refine, "refresh": refresh}
    if add_column > 0:
        scores["add_column"] = add_column
    if not any(v > 0 for v in scores.values()):
        return "query", 0.0
    # Ties resolve toward the safer intent. Writing a value is the harder act to
    # undo and the easier one to be wrong about, so a sentence that reads equally
    # as a read and a write is read.
    order = ("query", "add_column", "refresh", "backfill", "refine")
    best = max(order, key=lambda k: (scores.get(k, 0.0), -order.index(k)))
    return best, scores[best]


#: Wording that means "a field which does not exist yet". Kept narrow: a field
#: that already exists and is merely empty is a backfill, and treating the two
#: the same would rewrite the schema on a sentence that only asked for values.
_NEW_COLUMN_HINTS = (
    "new column", "new field", "another column", "another field", "extra column",
    "extra field", "add a column", "add a field", "add column", "add field",
    "track", "capture a", "also capture", "column for", "field for",
)


def _asks_for_new_column(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    return any(h in low for h in _NEW_COLUMN_HINTS)


def _mentioned_fields(question: str, known: list[str]) -> list[str]:
    """Which known fields the sentence names, matched on word boundaries.

    Substring matching would count `funding` when someone wrote `funding_round`
    is missing as a different field, and — worse — would happily match a field
    name that is a prefix of a longer one it is not.
    """
    words = set(_tokens(question))
    out = []
    for name in known:
        parts = [p for p in re.split(r"[_\W]+", str(name).lower()) if p]
        if not parts:
            continue
        if name.lower() in words or all(p in words for p in parts):
            out.append(name)
    return out


def _mentioned_hosts(text: str) -> list[str]:
    """Dotted tokens in a sentence, which is how a source gets named in words.

    Only shapes that can be a host are kept. A bare word like "the" is not a
    host, and offering it to the refresh path would produce a proposal that
    names a source nobody has.
    """
    out = []
    for t in _tokens(text):
        if "." in t and len(t) > 4 and not t.endswith("."):
            out.append(t)
    return sorted(set(out))


def _refresh_proposal(store, dataset_id: str, text: str, score: float,
                      named: list[str]) -> dict:
    from app.services import refresh as refresh_svc
    try:
        block = refresh_svc.propose(store, dataset_id, named or None)
    except Exception:  # noqa: BLE001
        block = {"refreshable": False, "reason": "this dataset's sources could not be read"}
    if not block.get("refreshable"):
        return {
            "intent": "refresh",
            "question": text,
            "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
            "action": "refresh_sources",
            "endpoint": f"POST /api/datasets/{dataset_id}/refresh",
            "sources": 0,
            "summary": "Nothing to re-read.",
            "reason": block.get("reason", ""),
            "writes": False,
            "needs_confirmation": False,
        }
    hosts = ", ".join(block.get("hosts") or []) or "its highest-yield sources"
    return {
        "intent": "refresh",
        "question": text,
        "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
        "action": "refresh_sources",
        "endpoint": f"POST /api/datasets/{dataset_id}/refresh",
        "hosts": block.get("hosts") or [],
        "sources": block.get("sources", 0),
        "cells_reverified": block.get("cells_reverified", 0),
        "cost": {"pages": block.get("sources", 0),
                 "extractions": block.get("estimated_extractions", 0),
                 "judge_calls": block.get("estimated_judge_calls", 0)},
        "summary": (
            f"Re-fetch {block.get('sources', 0)} source(s) ({hosts}) and re-verify the "
            f"{block.get('cells_reverified', 0)} value(s) they supplied. A value is "
            f"replaced only if the new one carries its own quote, and the value it "
            f"replaces is kept as a reviewable rival rather than discarded."),
        "writes": True,
        "needs_confirmation": True,
    }


def _add_column_proposal(store, dataset_id: str, text: str, score: float,
                         backfill_svc) -> dict:
    """Declare a field the schema lacks, then fill it from stored pages.

    Two writes, reported as one request because the user asked for one outcome:
    the schema gains a column, and backfill then treats it as a never-extracted
    field and fills it like any other. The proposal says which column and what it
    would cost, and the two steps are applied in that order by the caller — a
    column declared and left empty would look like a bug on the next page load.
    """
    schema = (store.get_dataset(dataset_id) or {}).get("schema") or []
    known = {str(f.get("name")).lower() for f in schema
             if isinstance(f, dict) and f.get("name")}
    # The column name is not invented from the sentence. It is taken from the
    # plan when the plan has an obvious candidate, and otherwise the request is
    # reported as needing a name — a schema with a guessed field is worse than no
    # schema, because the guess is what the next reader will trust.
    candidate = ""
    for f in schema:
        if isinstance(f, dict) and f.get("name"):
            known.add(str(f["name"]).lower())
    for name in _mentioned_fields(text, sorted(known)):
        if name.lower() not in known:
            candidate = name
    if not candidate:
        return {
            "intent": "add_column",
            "question": text,
            "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
            "action": "add_column",
            "endpoint": f"POST /api/datasets/{dataset_id}/backfill",
            "summary": ("I can add a column, but I will not guess its name. "
                        "Name the field and this becomes a concrete proposal."),
            "reason": "no field name could be read from the request",
            "writes": False,
            "needs_confirmation": False,
        }
    return {
        "intent": "add_column",
        "question": text,
        "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
        "action": "add_column",
        "endpoint": f"POST /api/datasets/{dataset_id}/backfill",
        "fields": [candidate],
        "declares_field": candidate,
        "summary": (
            f"Add `{candidate}` to this dataset's schema, then backfill it from the "
            f"pages this run already stored. The schema write and the value write "
            f"are separate steps and both are confirmed before either happens."),
        "writes": True,
        "needs_confirmation": True,
    }


def propose(store, question: str, dataset_id: str = "") -> dict:
    """Read-only. Returns a proposal the caller can show and then approve.

    Nothing here writes, and nothing here interprets the sentence as a command.
    Every proposal names the existing path that will do the work when approved,
    so "approve" has a definite meaning.
    """
    from app.services import backfill as backfill_svc
    from app.services import refresh as refresh_svc

    text = (question or "").strip()
    if not text:
        raise ValueError("say what you want changed or asked")

    intent, score = classify(text)
    if not intent:
        intent = "query"

    if intent == "query":
        return {
            "intent": "query",
            "question": text,
            "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
            "action": "run_query",
            "endpoint": f"POST /api/datasets/{dataset_id}/query",
            "summary": "Runs the read-only SQL path and shows the query it wrote.",
            "writes": False,
            "needs_confirmation": False,
        }

    if intent == "refresh":
        # A refresh names sources or pages; a sentence that names neither gets
        # the highest-yield sources, which is a choice the user can see in the
        # proposal rather than one made for them.
        named = _mentioned_hosts(text)
        return _refresh_proposal(store, dataset_id, text, score, named)

    if intent == "add_column":
        return _add_column_proposal(store, dataset_id, text, score, backfill_svc)

    if intent == "backfill":
        plan = backfill_svc.plan_for_dataset(store, dataset_id)
        schema = (store.get_dataset(dataset_id) or {}).get("schema") or plan.get("fields", [])
        known = [str(f.get("name")) for f in schema if isinstance(f, dict) and f.get("name")]
        named = _mentioned_fields(text, known)
        coverage = backfill_svc.propose(store, dataset_id, named or None)
        returnable = coverage.get("backfillable")
        summary = (
            f"Backfill {', '.join(coverage.get('fields') or []) or 'nothing'} from "
            f"{coverage.get('pages', 0)} stored page(s) this run already has."
        )
        if named and not coverage.get("fields"):
            summary = (
                f"None of {', '.join(named)} is entirely absent. Backfill only fills "
                f"fields no record has; a field some records carry is a coverage "
                f"gap, and overriding a value is a judgement, not a backfill.")
        return {
            "intent": "backfill",
            "question": text,
            "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
            "action": "backfill",
            "endpoint": f"POST /api/datasets/{dataset_id}/backfill",
            "fields": coverage.get("fields") or [],
            "backfillable": returnable,
            "cost": {"pages": coverage.get("pages", 0),
                     "extractions": coverage.get("estimated_extractions", 0),
                     "judge_calls": coverage.get("estimated_judge_calls", 0)},
            "summary": summary,
            "reason": coverage.get("reason", ""),
            "writes": True,
            # A write to real extracted data is always confirmed, and the dry
            # run is the default, so approving is a second act after seeing.
            "needs_confirmation": True,
        }

    try:
        plan = backfill_svc.plan_for_dataset(store, dataset_id)
    except backfill_svc.BackfillRefused:
        # A plan that cannot be recovered is not a reason to refuse the
        # question: the refine path takes a plan_id and reads it itself.
        plan = None
    return {
        "intent": "refine",
        "question": text,
        "confidence": round(min(1.0, 0.5 + score * 0.1), 2),
        "action": "refine_plan",
        "endpoint": "/api/workflows/refine",
        "plan_fields": len((plan or {}).get("fields", [])),
        "summary": ("Rewrites the plan this dataset came from and shows a "
                    "field-level diff. Compiles only — nothing is crawled until "
                    "a run is started."),
        "writes": True,
        "needs_confirmation": True,
    }
