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
           "parse_llm_json", "locate_quote", "normalize_for_match",
           "normalize_with_map"]

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
    raw Pydantic dump - smaller prompts, fewer schema-misread failures.

    A JSON field carrying `"description": null` yields an empty string, not the
    four-letter literal "None". `dict.get(k, "")` does not help there: the default
    only fires when the key is absent, and `str(None)` is "None". That string then
    reached the extraction prompt as the field's own description, which reads to a
    model as "emit the text None here".
    """
    out: dict[str, dict] = {}
    for f in fields_spec or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        desc = f.get("description")
        if desc is None:
            desc = ""
        out[str(f["name"])] = {"type": str(f.get("type") or "string"),
                               "description": str(desc),
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


#: Inline markdown emphasis and code markers.
#:
#: A rendered page is reduced to markdown, so the STORED text carries `**`, `*`,
#: `_` and backticks that the model does not reproduce when it quotes. Asked to
#: quote a company name off `**Company:** C.H. Robinson`, a model returns
#: `Company: C.H. Robinson` -- and neither the exact, case-insensitive nor
#: whitespace-collapsed search finds that, so a required field is ruled
#: unevidenced and the whole record is dropped.
#:
#: Measured on a live run: `supplychaindigital.com/top10/top-10-logistics-ceos`
#: is delivered by Chromium (markdown, markers present) and lost 13 of 13
#: extracted rows, every one with "required field(s) not evidenced:
#: company_name". The two plain-HTTP pages in the same run lost none. The
#: difference was the emphasis markers, not the content.
#:
#: Only these three characters are folded, and they are folded on BOTH sides of
#: every comparison, so the fold is symmetric and cannot invent a match between
#: two strings that share no characters. A quote that spans a markdown LINK
#: (`[text](url)`) is deliberately NOT rescued: mapping the inner text back
#: across its own delimiters is ambiguous, and a wrong offset is worse than a
#: refused one.
_MD_MARKERS = "*_`"


def normalize_with_map(text: object) -> tuple[str, list[int]]:
    """Lowercase, collapse whitespace, drop inline markdown markers.

    Returns the normalised string plus, for each character in it, that
    character's index in the original -- so a match found in normalised space
    can be reported as offsets into the original text.
    """
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # also collapses a leading run
    for i, ch in enumerate(str(text or "")):
        if ch in _MD_MARKERS:
            continue
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
        else:
            out.append(ch.lower())
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def normalize_for_match(text: object) -> str:
    """The one normalisation the support gate, the judge pre-check and the
    locator must share.

    They must agree or a field is admitted by one and refused by another: a
    quote the gate accepts and the locator cannot place produces `verified` with
    no offsets, and a quote the gate accepts but the judge's own substring check
    rejects produces a nulled value. Both are the same class of bug and this is
    the single definition they all read.
    """
    return normalize_with_map(text)[0]


def locate_quote(quote: str, source_text: str) -> tuple[int | None, int | None]:
    """Character offsets of quote within source_text, or (None, None).

    Exact first, then case-insensitive, then whitespace-collapsed, then
    markdown-folded. The third pass exists because the verification gate matches
    a quote against the source with whitespace collapsed - a quote the model
    emitted as one line matches a span laid out over several. The fourth exists
    because a rendered page's stored text carries emphasis markers the model
    drops when it quotes. The gate and the locator must agree, or a field is
    admitted as verified and then handed offsets of (None, None), which makes
    the citation unusable.

    Offsets address the ORIGINAL text. The markdown pass can therefore return a
    span that CONTAINS markers the quote did not have (`**Company:** X` for the
    quote `Company: X`), which is why `validator.settle` stores the original
    slice as the cell's quote rather than the model's spelling -- so the stored
    quote is always exactly the text at the stored offsets.
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
    if npos >= 0 and imap:
        s_idx = imap[npos]
        e_pos = npos + len(nq) - 1
        e_idx = imap[min(e_pos, len(imap) - 1)]
        return s_idx, e_idx + 1
    # Markdown-folded search, mapped back to original offsets. Reached only when
    # every pass above failed, so exact and whitespace matches keep priority.
    nq_md, _ = normalize_with_map(q)
    if not nq_md:
        return None, None
    src_md, imap_md = normalize_with_map(src)
    mpos = src_md.find(nq_md)
    if mpos < 0 or not imap_md:
        return None, None
    s_idx = imap_md[mpos]
    e_pos = min(mpos + len(nq_md) - 1, len(imap_md) - 1)
    return s_idx, imap_md[e_pos] + 1
