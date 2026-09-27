"""Phase-1 evidence contract: the canonical shape of proof-carrying extraction.

Every record the EXTRACT stage produces must validate against ExtractionEnvelope:
fields plus per-field evidence carrying a verbatim quote, source URL, optional
citation reference (the <n> marker from Crawl4AI references), and character
offsets locating the quote in the source text (filled by the validator).

Also home to the deterministic helpers the evidence chain needs:
simplify_schema (LLM-facing schema view), coverage_tripwire (pre-LLM
hallucination tripwire), parse_llm_json (tolerant JSON recovery),
locate_quote (offset calculation). No network, no LLM calls here — pure logic.
"""
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["EvidenceItem", "ExtractedRecord", "ExtractionEnvelope",
           "simplify_schema", "expected_terms", "coverage_tripwire",
           "parse_llm_json", "locate_quote"]

Coverage = Literal["full", "partial", "none"]


class EvidenceItem(BaseModel):
    field: str = ""
    value: Any | None = None
    quote: str = ""
    source_url: str = ""
    reference_id: str = ""


class ExtractedRecord(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class ExtractionEnvelope(BaseModel):
    records: list[ExtractedRecord] = Field(default_factory=list)
    coverage: Coverage = "none"


def simplify_schema(fields_spec: list[dict]) -> dict[str, dict]:
    """LLM-facing schema view: {name: {type, description}}.

    Shows the model the simplified contract (transform_schema pattern), never a
    raw Pydantic dump — smaller prompts, fewer schema-misread failures.
    """
    out: dict[str, dict] = {}
    for f in fields_spec or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        out[str(f["name"])] = {"type": str(f.get("type", "string")),
                               "description": str(f.get("description", "")),
                               "required": bool(f.get("required", False))}
    return out


_STOPWORDS = frozenset(
    "extract extraction website webpage page information data find get list "
    "names name value values record records item items company companies "
    "field fields required optional with from for the and verbatim source "
    "quote quotes evidence please following content text return output json "
    "array string number boolean date url null none any all each every per "
    "also plus total count use used using include includes including based "
    "about into over under between within without within may might must "
    "should would could shall will can this that these those then than are "
    "was were been being have has had does did not nor but yet such only "
    "very just more most other some such than too also description type".split())


def _split_terms(name: str) -> list[str]:
    parts = re.split(r"[_\-\s]+", str(name or ""))
    out: list[str] = []
    for p in parts:
        # split camelCase: FounderName -> Founder + Name
        for tok in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", p):
            t = tok.lower()
            if len(t) > 2 and t not in _STOPWORDS:
                out.append(t)
    return out


def expected_terms(fields_spec: list[dict], goal: str = "") -> list[str]:
    """Terms the source text should contain if it can satisfy this plan.

    Field names/descriptions split on snake_case/camelCase with stopwords
    dropped, plus significant goal words. Deterministic; feeds the tripwire.
    """
    terms: list[str] = []
    for f in fields_spec or []:
        if not isinstance(f, dict):
            continue
        for t in _split_terms(f.get("name", "")) + _split_terms(f.get("description", "")):
            if t not in terms:
                terms.append(t)
    for w in re.findall(r"[a-z]{3,}", (goal or "").lower()):
        if w not in _STOPWORDS and w not in terms:
            terms.append(w)
    return terms


def coverage_tripwire(fields_spec: list[dict], text: str, goal: str = "") -> dict:
    """Pre-LLM hallucination tripwire: do ANY expected terms appear in the text?

    Returns {matched, missing, covered}. covered=False (zero expected terms in
    the text) means the source is likely an error page, JS shell, or wrong
    section — the model would almost certainly answer NA or confabulate, so
    the extractor skips the bounded regen (but still attempts once; absence of
    terms is a warning, not proof of absence). Deliberately conservative:
    a single term match clears the tripwire.
    """
    terms = expected_terms(fields_spec, goal)
    low = (text or "").lower()
    matched = [t for t in terms if t in low]
    missing = [t for t in terms if t not in matched]
    return {"matched": matched, "missing": missing, "covered": bool(matched)}


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def parse_llm_json(text: str) -> Any:
    """Tolerant JSON recovery for model output. Raises ValueError when unusable.

    Ladder: raw parse -> fence-stripped parse -> doubled-brace-stripped parse
    (some models echo {{"records": ...}}). Never returns partial guesses: if
    all rungs fail the caller treats the page as unextractable, not as empty.
    """
    last: Exception | None = None
    candidates = [text, _strip_fences(text)]
    stripped = _strip_fences(text)
    if stripped.startswith("{{"):
        candidates.append(stripped.replace("{{", "{").replace("}}", "}"))
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception as e:  # noqa: BLE001 (ladder continues)
            last = e
    raise ValueError(f"LLM returned non-JSON output: {str(last)[:120]}")


def _norm_with_map(src: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed `src` plus, for each collapsed char, its index in
    the original. Needed because the verification gate matches quotes with
    whitespace collapsed, so the locator has to agree with it."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # also collapses a leading run
    for i, ch in enumerate(src):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def locate_quote(quote: str, source_text: str) -> tuple[int | None, int | None]:
    """Character offsets of quote within source_text, or (None, None).

    Exact match first, then case-insensitive, then whitespace-collapsed. The
    third pass exists because the verification gate matches a quote against the
    source with whitespace collapsed - a quote the model emitted as one line
    matches a span laid out over several. The gate and the locator must agree,
    or a field is admitted as verified and then handed offsets of (None, None),
    which makes the citation unusable. Offsets address the ORIGINAL text.
    """
    q = quote or ""
    src = source_text or ""
    if not q or not src:
        return None, None
    start = src.find(q)
    if start >= 0:
        return start, start + len(q)
    low = src.lower().find(q.lower())
    if low >= 0:
        return low, low + len(q)
    # Whitespace-collapsed search, mapped back to original offsets.
    nq = " ".join(q.split())
    if not nq:
        return None, None
    norm_src, imap = _norm_with_map(src.lower())
    npos = norm_src.find(nq.lower())
    if npos < 0 or not imap:
        return None, None
    s_idx = imap[npos]
    e_pos = npos + len(nq) - 1
    e_idx = imap[min(e_pos, len(imap) - 1)]
    return s_idx, e_idx + 1
