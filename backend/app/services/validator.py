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
#: Currency symbols and magnitude suffixes a scraped number realistically carries.
_NUM_CLEAN = re.compile(r"[,$€£¥%\s ~]")
_NUM_SUFFIX = re.compile(r"(?i)\s*([kmbt])?\s*(illion)?$")
_NUM_RE = re.compile(r"^-?\d*\.?\d+([kmbt])?$", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _norm(s: object) -> str:
    return _WS.sub(" ", str(s or "").strip().lower())


def type_ok(value: object, ftype: str) -> bool:
    """Does the value actually match the field's declared type?

    `required` and `type` used to be discarded on the grounds that "type
    coercion lives in normalize" - but normalize only *transforms* a value and
    never reports failure, so a string where a number was declared sailed
    through as verified. This is the check that was missing.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return True  # emptiness is `required`'s business, not the type's
    t = (ftype or "string").strip().lower()
    if t in ("string", "text", ""):
        return True
    if t == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        # Scraped numbers arrive as "$2.4B", "1,200", "45%", "~30". Accept those
        # shapes; reject prose. An earlier version was strict enough to call
        # "$2.4B" a type error, which would have dropped real records at the
        # gate rather than catching anything.
        s = _NUM_CLEAN.sub("", str(value)).strip()
        s = re.sub(r"(?i)(illion)$", "", s)
        return bool(_NUM_RE.match(s))
    if t == "boolean":
        return isinstance(value, bool) or str(value).strip().lower() in (
            "true", "false", "yes", "no", "0", "1")
    if t == "array":
        return isinstance(value, (list, tuple))
    if t == "date":
        s = str(value).strip()
        if re.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$", s):
            return True
        low = s.lower()
        if not any(m in low for m in _MONTHS):
            return False
        # A month name plus a year, with or without a day: "January 2024",
        # "Jan 2024", "January 31, 2024", "31 January 2024".
        return bool(re.search(r"\b(19|20)\d{2}\b", s))
    if t == "url":
        return bool(re.match(r"^https?://\S+$", str(value).strip(), re.I))
    return True


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


class JudgeBudget:
    """Bounds judge calls within one run.

    Verification asks a judge about every field of every extracted record, and
    the record count is whatever the extractor found - a live run hit 728, which
    is thousands of round trips and a certain budget overrun. Once the cap is
    spent the answer is JUDGMENT_UNAVAILABLE: the value is still kept, it is
    just never counted as verified, which is the honest description of a check
    that never ran.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit or 0))
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def spend(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        return True


#: Per-run judge budget. Replaced by execute_run via reset_judge_budget().
JUDGE_BUDGET = JudgeBudget(0)


async def verify_field(value: object, quote: str, source_text: str,
                       required: bool, ftype: str, reference_id: str = "",
                       references: str = "") -> tuple[object | None, str]:
    """Grade one field. Returns (value_to_store, status).

    Order matters: the cheap deterministic gates come first, the judge last,
    because the judge is the only step that can be unavailable and the only one
    that costs a round trip. Statuses:

      unverified           the claim is not supported by a quotable page
      judgment_unavailable the quote IS in the page, but nothing judged it
      verified             judged, or deduced from the quote itself
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "unverified"
    if not type_ok(value, ftype):
        return None, "unverified"
    if not quote or _norm(quote) not in _norm(source_text):
        return None, "unverified"
    if not reference_known(reference_id, references):
        return None, "unverified"
    if JUDGE_BUDGET.exhausted:
        # The cap is spent. Keep the value - the quote genuinely is on the page
        # - but do not claim a ruling that never happened.
        return value, "judgment_unavailable"
    if not JUDGE_BUDGET.spend():
        return value, "judgment_unavailable"
    verdict = await jev.evidence_verification(str(value), quote, source_text)
    judgment = verdict.get("judgment")
    if judgment == "NOT_SUPPORTED":
        return None, "unverified"
    if judgment == "JUDGMENT_UNAVAILABLE":
        # Keep the value - the quote genuinely is on the page - but never count
        # it as verified. Reporting this as verified is what made a dead judge
        # look like a passing one.
        return value, "judgment_unavailable"
    if judgment == "SUPPORTED":
        return value, "verified"
    # UNCERTAIN, or anything a judge returned that we cannot interpret: the
    # value stays, but an unproven claim is not a verified one.
    return value, "judgment_unavailable"


async def wrap_record(fields_spec: list[dict], raw: dict, source_text: str,
                      source_url: str, source_title: str, now: str = "",
                      references: str = "", page_id: str = "") -> dict:
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
                         # page_id makes the claim re-checkable: the offsets
                         # above address stored text rather than a string that
                         # only existed in this process's memory.
                         "source": {"url": source_url, "title": source_title,
                                    "quote": quote, "retrieved_at": ts,
                                    "reference_id": ref_id,
                                    "page_id": page_id or ev.get("page_id", ""),
                                    "content_hash": ev.get("content_hash", ""),
                                    "start": start, "end": end}}
    # Shape-drift guard: every wrapped row must satisfy the RecordRow contract.
    # Return the inner fields dict (callers expect {name: provenance-field}).
    return RecordRow.model_validate({"fields": wrapped}).model_dump()["fields"]
