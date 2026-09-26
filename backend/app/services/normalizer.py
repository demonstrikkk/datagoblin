"""Normalization: currency/date/URL/company/phone. Original always preserved;
normalized{} added alongside (Evidence layer, docs/29). MVP Python; Polars Phase 2."""
import re
from urllib.parse import urlparse

_MONEY = re.compile(r"([\d,.]+)\s*(billion|million|thousand|[bmk])?", re.I)
_UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def normalize_currency(raw: object) -> dict:
    if not isinstance(raw, str):
        return {"original": raw, "normalized": raw}
    m = _MONEY.search(raw.replace(",", ""))
    if not m:
        return {"original": raw, "amount": None, "currency": None}
    try:
        num = float(m.group(1))
    except ValueError:
        return {"original": raw, "amount": None, "currency": None}
    mult = _UNIT.get((m.group(2) or "").lower(), 1)
    cur = "USD" if "$" in raw or "usd" in raw.lower() else None
    return {"original": raw, "amount": int(num * mult), "currency": cur}


def normalize_date(raw: object) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {"original": raw, "normalized": raw}
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y"):
        try:
            return {"original": raw, "normalized": datetime.strptime(raw.strip(), fmt).date().isoformat()}
        except ValueError:
            continue
    return {"original": raw, "normalized": raw}


def normalize_url(raw: object) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {"original": raw, "normalized": raw}
    u = raw.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        p = urlparse(u)
        host = p.hostname.lower() if p.hostname else ""
        path = p.path.rstrip("/") or ""
        return {"original": raw, "normalized": f"{p.scheme}://{host}{path}"}
    except Exception:
        return {"original": raw, "normalized": raw.strip()}


def normalize_phone(raw: object) -> dict:
    if not isinstance(raw, str):
        return {"original": raw, "normalized": raw}
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return {"original": raw, "normalized": raw}
    return {"original": raw, "normalized": "+" + digits}


def normalize_company(raw: object) -> dict:
    if not isinstance(raw, str):
        return {"original": raw, "normalized": raw}
    norm = re.sub(r"\b(ltd|inc|llc|pvt|private|limited|corp|co)\b\.?", "", raw.lower()).strip()
    return {"original": raw, "normalized": re.sub(r"\s+", " ", norm)}


def normalize_value(name: str, value: object) -> dict:
    n = name.lower()
    if isinstance(value, str) and ("$" in value or "usd" in value.lower() or "million" in value.lower()):
        return normalize_currency(value)
    if "date" in n or "founded" in n:
        return normalize_date(value)
    if "url" in n or "website" in n or "link" in n:
        return normalize_url(value)
    if "phone" in n or "tel" in n:
        return normalize_phone(value)
    if "company" in n or "name" in n:
        return normalize_company(value)
    if isinstance(value, str):
        return {"original": value, "normalized": value.strip()}
    return {"original": value, "normalized": value}


def normalize_record(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        base = dict(v) if isinstance(v, dict) else {"value": v}
        base["normalized"] = normalize_value(k, base.get("value"))
        out[k] = base
    return out
