"""Provenance helpers: single construction site for evidence-wrapped fields."""
import datetime


def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def field(value: object, status: str, url: str, title: str, quote: str,
          retrieved_at: str = "") -> dict:
    return {"value": value, "verification_status": status,
            "source": {"url": url, "title": title, "quote": quote,
                       "retrieved_at": retrieved_at or now_iso()}}


def summarize(rows: list[dict]) -> dict:
    """Record-level summary, plus field counts for detail.

    This used to report `{"records":1,"verified":0,"needs_review":4}`, which
    reads as four bad records when it is one record with four unproven fields.
    The counts are now named for what they measure, and a record is only
    `fully_verified` when every one of its fields is.
    """
    fully = partial = 0
    f_verified = f_unverified = f_unjudged = f_conflicting = f_throttled = 0
    for r in rows:
        statuses = [v.get("verification_status")
                    for v in r.get("fields", {}).values() if isinstance(v, dict)]
        for st in statuses:
            if st == "verified":
                f_verified += 1
            elif st == "rate_limited":
                f_throttled += 1
            elif st == "judgment_unavailable":
                f_unjudged += 1
            elif st == "conflicting":
                f_conflicting += 1
            else:
                f_unverified += 1
        if statuses and all(s == "verified" for s in statuses):
            fully += 1
        else:
            partial += 1
    return {"records": len(rows),
            "records_fully_verified": fully,
            "records_needing_review": partial,
            "fields_verified": f_verified,
            "fields_unverified": f_unverified,
            "fields_judgment_unavailable": f_unjudged,
            "fields_conflicting": f_conflicting,
            # Throttled is reported separately from unjudged: one is "come back
            # later", the other is "we had no judge to ask".
            "fields_rate_limited": f_throttled}
