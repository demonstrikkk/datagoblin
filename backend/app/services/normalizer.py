"""Normalization: currency/date/URL/company/phone. Original always preserved;
normalized{} added alongside (Evidence layer, docs/29). MVP Python; Polars Phase 2."""
import re
from urllib.parse import urlparse

_MONEY = re.compile(r"([\d,.]+)\s*(billion|million|thousand|[bmk])?", re.I)
_UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}

#: The characters a UTF-8 byte sequence turns into when it is decoded as
#: cp1252. If a string contains one of these it *may* be mojibake; the repair is
#: only accepted when it removes them all, so a genuinely odd value is left
#: alone rather than being "repaired" into something different.
_MOJIBAKE_LEADS = ("Ã", "Â", "â€", "â€™", "ðŸ", "Ð", "Ñ")

#: What a model writes when it means "there is no value here". The extraction
#: prompt says "NA" and the models obey it most of the time; when they do not,
#: the most common thing they send instead is a punctuation mark — an em dash,
#: an en dash, a curly quote, a lone question mark. A live run stored a 15-field
#: schema in which most cells were `”` or `—`, which rendered in the UI as
#: `â€"` and read as though the dataset were full of garbage rather than empty.
_ABSENCE_WORDS = frozenset({
    "na", "n/a", "n.a.", "n.a", "nan", "null", "nil", "none", "unknown",
    "unspecified", "not specified", "not available", "not found",
    "not provided", "not applicable", "not listed", "tbd", "tba", "xxx",
    "no data", "no value", "empty", "missing",
})


def repair_mojibake(value: object) -> object:
    """Undo a UTF-8-bytes-read-as-cp1252 round trip, when that is what happened.

    Returns the value untouched unless every one of these holds: it is a string,
    it contains a lead character that cp1252-mangling produces, it re-encodes to
    cp1252 without error, and it decodes back as UTF-8 into a string with no
    lead characters left. Anything less and the original is returned, because a
    wrong repair would silently change a real value — which for a dataset whose
    claim is that every value is evidence-backed is the one unacceptable outcome.
    """
    if not isinstance(value, str) or not any(c in value for c in _MOJIBAKE_LEADS):
        return value
    try:
        fixed = value.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
        return value
    if any(c in fixed for c in _MOJIBAKE_LEADS):
        return value
    return fixed


def is_placeholder(value: object) -> bool:
    """True when this value carries no information at all, so it is not data.

    Two kinds count, and both are things a model emits rather than things a
    source says:

    * no alphanumeric character at all — `—`, `–`, `-`, `”`, `"`, `…`, `?`;
    * an explicit absence word — `N/A`, `null`, `unknown`, `tbd`.

    A value that is merely *unverified* is not a placeholder and is kept; that
    distinction is the whole point of the verification statuses, and collapsing
    the two would delete real findings. `0` and `False` are values, not
    absences: zero employees is an answer.

    Mojibake is judged on its *residue*, not only on a successful repair. Some
    corruptions cannot be repaired: UTF-8 byte 0x9D is undefined in cp1252, so
    the mangled form of `”` is `â€` followed by a raw C1 control that Python's
    cp1252 codec refuses to encode. Testing the repaired string alone would let
    that one through, because its leading `â` is a letter and would read as
    content. Dropping the lead characters and asking whether anything is left
    catches it, and cannot mistake real text for an absence — `CafÃ©` still has
    `Café` underneath.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    s = repair_mojibake(value).strip()
    if not s:
        return True
    residue = s
    for lead in _MOJIBAKE_LEADS:
        residue = residue.replace(lead, "")
    if not any(ch.isalnum() for ch in residue):
        return True
    return s.casefold() in _ABSENCE_WORDS


def clean_value(value: object) -> object:
    """The value as it should be stored: mojibake repaired, whitespace trimmed."""
    if isinstance(value, str):
        return repair_mojibake(value).strip()
    return value



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
