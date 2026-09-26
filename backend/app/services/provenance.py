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
    verified = needs = 0
    for r in rows:
        for v in r.get("fields", {}).values():
            if isinstance(v, dict) and v.get("verification_status") == "verified":
                verified += 1
            else:
                needs += 1
    return {"records": len(rows), "verified": verified, "needs_review": needs}
