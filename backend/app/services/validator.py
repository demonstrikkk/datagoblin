"""Validate: substring pre-check (cheap) -> reference gate -> Jev-B -> policy -> wrap.

Status precedence: substring-fail => unverified (never stored guess);
reference_id unknown => unverified; Jev NOT_SUPPORTED => unverified;
conflict (Jev-C) => conflicting with both provenances.

wrap_record pins each verified quote to character offsets in the preserved
source text and carries the citation reference_id — the machine-checkable
half of the proof-carrying record.
"""
import datetime
import re

from app.providers.decision import jev
from app.schemas.evidence import locate_quote

_WS = re.compile(r"\s+")


def _norm(s: object) -> str:
    return _WS.sub(" ", str(s or "").strip().lower())


def normalize_key(s: object) -> str:
    s = _norm(s)
    s = re.sub(r"\b(ltd|inc|llc|pvt|private|limited|corp|co)\b", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def reference_known(reference_id: str, references: str) -> bool:
    """A claimed <n> citation must exist in the page's References map.

    Empty reference_id is allowed (static/uncited sources); a non-empty one
    that appears nowhere in the references block fails closed.
    """
    ref = (reference_id or "").strip()
    if not ref:
        return True
    return ref in (references or "")


async def verify_field(value: object, quote: str, source_text: str,
                       required: bool, ftype: str, reference_id: str = "",
                       references: str = "") -> tuple[object | None, str]:
    _ = required, ftype  # type coercion lives in normalize; here: evidence only
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "unverified"
    if not quote or _norm(quote) not in _norm(source_text):
        return None, "unverified"
    if not reference_known(reference_id, references):
        return None, "unverified"
    verdict = await jev.evidence_verification(str(value), quote, source_text)
    if verdict["judgment"] == "NOT_SUPPORTED":
        return None, "unverified"
    return value, "verified"


async def wrap_record(fields_spec: list[dict], raw: dict, source_text: str,
                      source_url: str, source_title: str, now: str = "",
                      references: str = "") -> dict:
    from app.schemas.run import RecordRow  # local import: schemas must not import services

    ts = now or (datetime.datetime.utcnow().isoformat() + "Z")
    wrapped: dict = {}
    for f in fields_spec:
        name = f["name"]
        ev = next((e for e in raw.get("evidence", []) if e.get("field") == name), {})
        quote = ev.get("quote", "")
        ref_id = ev.get("reference_id", "")
        v, st = await verify_field(raw.get("fields", {}).get(name), quote,
                                   source_text, f.get("required", False),
                                   f.get("type", "string"), ref_id, references)
        start, end = locate_quote(quote, source_text) if st == "verified" else (None, None)
        wrapped[name] = {"value": v, "verification_status": st,
                         "source": {"url": source_url, "title": source_title,
                                    "quote": quote, "retrieved_at": ts,
                                    "reference_id": ref_id,
                                    "start": start, "end": end}}
    # Shape-drift guard: every wrapped row must satisfy the RecordRow contract.
    # Return the inner fields dict (callers expect {name: provenance-field}).
    return RecordRow.model_validate({"fields": wrapped}).model_dump()["fields"]
