"""Turn a sentence into an executable proposal. Never into a mutation.

The point of asking a question in words is that you should not have to know
which endpoint fills the gap you noticed. But the answer to "add the funding
round" must not be a language model writing into a dataset — that is how a
dataset whose entire claim is *every value carries a quote* stops meaning it.

So the shape is always the same, and it is deliberate:

    sentence -> classified intent -> a concrete proposal the user can read
             -> explicit approval -> the existing verified path executes it

Three intents, because those are the three things actually worth doing to a
dataset:

* ``query``     — answer a question by running the guarded SQL path.
* ``backfill``  — name fields that no record has and can be filled from stored
  pages, and say what it would cost before doing it.
* ``refine``    — change the plan the dataset came from and show a field diff.

There is no fourth. A proposal that cannot be classified is reported as
unrecognised along with the three that exist, rather than being interpreted
loosely. Being told "I did not understand that" is the correct answer far more
often than forcing a sentence into one of these three, and it is the only
answer that cannot quietly do the wrong thing.
"""
from __future__ import annotations

import re

#: What the user asked for, and how each is carried out. `run` is the name of
#: the existing, already-verified path each intent delegates to — this module
#: never performs the work itself.
INTENTS = ("query", "backfill", "refine")

_BACKFILL_HINTS = (
    "add", "fill", "missing", "backfill", "empty", "blank", "capture",
    "extract", "get", "need", "want", "also", "plus",
)
_BACKFILL_TARGETS = (
    "field", "column", "value", "data", "info", "detail", "details", "round",
    "founder", "funding", "valuation", "size", "industry", "city", "country",
    "year", "ticker", "employee", "hq", "url", "website", "salary", "price",
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

    if backfill <= 0 and refine <= 0:
        return "query", 0.0
    if backfill >= refine:
        return "backfill", backfill
    return "refine", refine


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


def propose(store, question: str, dataset_id: str = "") -> dict:
    """Read-only. Returns a proposal the caller can show and then approve.

    Nothing here writes, and nothing here interprets the sentence as a command.
    Every proposal names the existing path that will do the work when approved,
    so "approve" has a definite meaning.
    """
    from app.services import backfill as backfill_svc

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
