"""Cross-run trust in a source, and how stale a dataset is.

Two questions this answers that nothing else in the codebase did:

1. **Is this host worth spending a request on?** Every other ranking in this
   project is scoped to one dataset. `yieldmap` ranks hosts by how much they
   yielded for *this* dataset, which is the right question when re-reading that
   dataset, and the wrong one when choosing where to crawl next: a host that was
   authoritative two years ago, or one that has started returning 403s, ranks
   identically to one that answered perfectly this morning.

2. **How old is this?** Nothing tracked age. `pages.retrieved_at` records when a
   page was fetched, but a dataset is only as fresh as its *weakest* verified
   field, and there was no way to ask which field that was. A dataset whose
   company names were verified last week and whose revenue figures were verified
   last year read as one undifferentiated age.

Design notes worth knowing before changing anything:

* **Scores are derived, not stored.** `source_reputation` keeps raw counts and
  `last_seen`; `trust()` computes a number from them. A stored score cannot be
  recomputed when the decay policy changes without rewriting every row, and the
  decay policy *will* change — it is a judgement call, not a constant.

* **Decay is applied to the *evidence*, not to the arithmetic.** A host that
  proved 900 of 1000 cells two years ago and nothing since is not trusted as
  highly as one that proved 900 yesterday, but it is not discarded either. The
  half-life halves the weight of old evidence rather than erasing it, because
  "we stopped using this source" and "this source was bad" are different facts.

* **Unproven is not the same as absent.** A host that has answered five times and
  proven nothing is scored, not skipped: it may be a good source for a question it
  simply cannot answer yet.

* **No network, no model, no budget.** This is arithmetic on stored counts. It
  runs on every refresh candidate and must cost nothing.
"""
from __future__ import annotations

import datetime
from typing import Any

#: Days after which a host's evidence counts half as much.
#:
#: Chosen against the actual refresh cadence rather than picked for looks. The
#: default `RUN_MAX_RUNTIME_S` is 25 minutes and refresh is manual, so a dataset
#: is re-read every few days to weeks in normal use. A half-life of 30 days means
#: evidence from the previous month still carries about half its weight, and a
#: source untouched for a year has decayed to roughly 0.2% — present in the record
#: and effectively irrelevant to the score.
DECAY_HALF_LIFE_DAYS = 30.0

#: Ceiling for the block penalty. A host blocked 200 times is not 200x worse than
#: one blocked once, or it would sit at the bottom forever and never be retried
#: — which is its own failure, since a temporary block is exactly the thing worth
#: retrying later.
MAX_BLOCK_PENALTY = 0.5

#: A host with fewer than this many visits has not earned an opinion. Below it the
#: score is pulled toward the neutral midpoint rather than computed from one or two
#: lucky pages.
MIN_VISITS_FOR_CONFIDENCE = 3


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _as_datetime(value: Any) -> datetime.datetime | None:
    """Coerce a stored timestamp to an aware datetime, or None.

    Repositories do not agree on the type. `psycopg` hands back a real
    `datetime` for `timestamptz`, but the same row read through the JSON
    serialisation path — and every value that has been through a response model
    — is an ISO string. Treating that as a datetime is the difference between a
    working staleness endpoint and an `AttributeError` inside it, so the coercion
    lives here rather than at each call site.

    Naive values are assumed UTC. The database stores UTC; a naive datetime is a
    value that lost its zone on the way out, and the alternative is refusing to
    compute an age for it at all.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(float(value), datetime.UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
    return None


def _age_days(when: datetime.datetime | None, now: datetime.datetime) -> float:
    if when is None:
        return 0.0
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _decay(when: Any, now: datetime.datetime) -> float:
    """Weight for evidence of a given age. 1.0 today, 0.5 at the half-life."""
    moment = _as_datetime(when)
    if moment is None:
        return 0.0
    if DECAY_HALF_LIFE_DAYS <= 0:
        return 1.0
    return 0.5 ** (_age_days(moment, now) / DECAY_HALF_LIFE_DAYS)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def trust(row: dict[str, Any], now: datetime.datetime | None = None) -> dict[str, Any]:
    """Score one host from its stored counters.

    Returns the score plus every term that produced it. An unexplainable number
    is not usable for a decision an operator may have to justify, and the terms
    are also what the UI shows when someone asks why a source was deprioritised.

    Components, in the order they can veto:

    * **proof** — verified_cells / cells, decayed. The dominant term. This is the
      only one that can make a host good.
    * **reachability** — successes / visits, decayed more slowly. A host that
      answers but proves nothing is not preferred, and a host that stops
      answering must drop out regardless of its history.
    * **block penalty** — subtracted, capped. "Refuses" is worse than "silent".
    """
    now = now or _now()
    visits = max(0, int(row.get("visits") or 0))
    successes = max(0, int(row.get("successes") or 0))
    cells = max(0, int(row.get("cells") or 0))
    verified = max(0, int(row.get("verified_cells") or 0))
    blocks = max(0, int(row.get("block_count") or 0))

    if visits == 0:
        return {"host": row.get("host", ""), "score": 0.0, "known": False,
                "reason": "never fetched", "terms": {}}

    fresh = _decay(row.get("last_success_at") or row.get("last_seen"), now)

    proof_ratio = (verified / cells) if cells else 0.0
    proof = _clamp(proof_ratio) * fresh

    reach_ratio = (successes / visits) if visits else 0.0
    reachability = _clamp(reach_ratio) * (0.5 + 0.5 * fresh)

    # Each block costs 10 points, to a floor of MAX_BLOCK_PENALTY. Ten is enough
    # to matter against a good proof score and small enough that a single throttle
    # does not bury an otherwise excellent host.
    block_penalty = min(MAX_BLOCK_PENALTY, blocks * 0.10)

    score = _clamp(0.7 * proof + 0.3 * reachability - block_penalty)

    # With too little history the raw ratio is one page's opinion. Pull it toward
    # the midpoint so a single lucky fetch cannot make a host outrank one that has
    # answered a hundred times.
    if visits < MIN_VISITS_FOR_CONFIDENCE:
        score = 0.5 * score + 0.5 * 0.25

    return {
        "host": row.get("host", ""),
        "score": round(score, 4),
        "known": True,
        "visits": visits,
        "cells": cells,
        "verified_cells": verified,
        "blocks": blocks,
        "terms": {
            "proof": round(proof, 4),
            "reachability": round(reachability, 4),
            "block_penalty": round(block_penalty, 4),
            "decay": round(fresh, 4),
            "low_confidence": visits < MIN_VISITS_FOR_CONFIDENCE,
        },
    }


def rank_hosts(rows: list[dict[str, Any]],
               now: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Highest trust first. Ties break on recency, then name, so the order is stable."""
    now = now or _now()
    scored = [trust(r, now) for r in rows]
    scored.sort(key=lambda s: (-s["score"],
                               -(s.get("terms", {}).get("decay") or 0.0),
                               s["host"]))
    return scored


# --------------------------------------------------------------------------
# staleness


#: How old a verification may be before it is called stale, per field name.
#:
#: Not a single global number. A company's legal name changes perhaps once a
#: decade; a funding round or a headcount changes monthly. Applying one threshold
#: to both means either stale numbers are presented as current, or stable names
#: are re-fetched forever.
#:
#: Matched on the field name, because that is the only signal available before
#: the value is read. Unlisted fields fall through to `DEFAULT_STALE_DAYS`.
FAST_CHANGING_HINTS = (
    "revenue", "funding", "raised", "amount", "valuation", "market_cap",
    "headcount", "employee_count", "price", "stock", "valuation", "cap",
    "date", "updated", "posted", "published", "status", "openings",
    "deadline", "closing", "series", "round", "arr",
)
STABLE_HINTS = (
    "name", "founded", "location", "hq", "website", "url", "domain",
    "industry", "sector", "description", "ceo", "founder", "owner", "parent",
)

#: 45 days: long enough that a dataset is not permanently "stale", short enough
#: that a year-old figure is not presented as current.
DEFAULT_STALE_DAYS = 45.0
FAST_STALE_DAYS = 14.0
STABLE_STALE_DAYS = 365.0


def stale_after_days(field_name: str) -> float:
    """How old a verification of this field may get before it is stale."""
    n = str(field_name or "").lower()
    if any(h in n for h in FAST_CHANGING_HINTS):
        return FAST_STALE_DAYS
    if any(h in n for h in STABLE_HINTS):
        return STABLE_STALE_DAYS
    return DEFAULT_STALE_DAYS


def field_age_days(row: dict[str, Any], now: datetime.datetime | None = None) -> float | None:
    """Days since this field was last verified. None means never verified."""
    now = now or _now()
    when = _as_datetime(row.get("last_verified_at"))
    if when is None:
        return None
    return _age_days(when, now)


def is_stale(row: dict[str, Any], now: datetime.datetime | None = None) -> bool:
    """Was this field verified too long ago to present as current?"""
    age = field_age_days(row, now)
    if age is None:
        # Never verified is the most stale thing there is, but it is a different
        # condition from "verified long ago" and the UI words them differently.
        return True
    return age > stale_after_days(str(row.get("field") or ""))


def dataset_staleness(fields: list[dict[str, Any]],
                       now: datetime.datetime | None = None) -> dict[str, Any]:
    """Summarise a dataset's freshness.

    Reported as a *distribution*, not one number. A single average hides the only
    thing that matters: that one field is two years old while the rest are fresh.
    `stale_fields` names them so the reader can see which.
    """
    now = now or _now()
    per_field: list[dict[str, Any]] = []
    stale: list[str] = []
    never: list[str] = []
    ages: list[float] = []

    for row in fields or []:
        name = str(row.get("field") or "")
        age = field_age_days(row, now)
        threshold = stale_after_days(name)
        is_old = is_stale(row, now)
        if age is None:
            never.append(name)
        else:
            ages.append(age)
        if is_old:
            stale.append(name)
        per_field.append({
            "field": name,
            "age_days": None if age is None else round(age, 1),
            "stale_after_days": threshold,
            "stale": is_old,
            "ever_verified": age is not None,
            "last_verified_at": row.get("last_verified_at"),
            "verified_cells": int(row.get("verified_cells") or 0),
            "total_cells": int(row.get("total_cells") or 0),
        })

    total = len(per_field)
    # Freshness is the *weakest* field's age, not the average: a dataset is only
    # as current as the number you would quote from it.
    worst = max(ages) if ages else None
    return {
        "field_count": total,
        "stale_count": len(stale),
        "stale_fields": sorted(stale),
        "never_verified_fields": sorted(never),
        "stale_pct": round(100.0 * len(stale) / total, 1) if total else 0.0,
        "oldest_age_days": None if worst is None else round(worst, 1),
        "median_age_days": (round(sorted(ages)[len(ages) // 2], 1)
                            if ages else None),
        "fields": per_field,
    }


def refresh_order(fields: list[dict[str, Any]],
                  now: datetime.datetime | None = None) -> list[str]:
    """Field names in the order a refresh should re-check them.

    Most overdue first, and never-verified outranks everything — an unverified
    field is the only one where a read is certain to add something.
    """
    now = now or _now()
    def key(row: dict[str, Any]):
        age = field_age_days(row, now)
        # None (never verified) sorts first because `float("inf")` is the largest.
        return (-(age if age is not None else float("inf")),
                str(row.get("field") or ""))
    return [str(r.get("field") or "") for r in sorted(fields or [], key=key)]