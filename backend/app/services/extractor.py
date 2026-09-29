"""Page -> proof-carrying records (Phase 1 evidence chain).

Pipeline per page: coverage tripwire (pre-LLM, fail-open warning) -> single or
chunk-parallel extract (bounded calls) -> deterministic merge -> ONE bounded
regen when the tripwire says content exists but nothing extracted -> coerce to
the ExtractionEnvelope contract. Empty list (never prose, never guesses).
"""
import asyncio

from app.core.config import settings
from app.core.logging import log
from app.schemas.evidence import (ExtractedRecord, coverage_tripwire,
                                  parse_llm_json, simplify_schema)
from app.services.normalizer import clean_value, is_placeholder

_CHUNK_CHARS = 6000
_CHUNK_OVERLAP = 600
_MAX_CHUNKS = 4  # 1 page costs at most 4 chunk calls + 1 regen

REGEN_PREAMBLE = (
    "You are an extractor and your previous attempt returned no records "
    "although the page contains the requested terms. Try again: scan the "
    "whole content, extract every matching record, and provide the missing "
    "fields. If a field value is absent, use \"NA\" — never invent values.")


def build_prompt(plan: dict, url: str, title: str, clean: str, part: str = "") -> str:
    schema = simplify_schema(plan.get("fields", []))
    lines = [f"{n}:{s['type']} — {s['description'] or n}"
             f"{' (REQUIRED)' if s['required'] else ' (optional)'}"
             for n, s in schema.items()]
    fields = "\n".join(lines) or "company_name:string — Name (REQUIRED)"
    head = f"Part {part} of the page. " if part else ""
    return (
        f"{head}Extract records as JSON {{\"records\": [{{\"fields\": {{...}}, "
        f"\"evidence\": [{{\"field\": name, \"value\": extracted value, "
        f"\"quote\": \"verbatim substring of the content below\", "
        f"\"source_url\": \"{url}\", "
        f"\"reference_id\": \"<n> citation marker when the quote comes from a "
        f"cited span, else empty string\"}}]}}], \"coverage\": "
        f"\"full\"|\"partial\"|\"none\"}}. No prose. No backticks. "
        f"Do not start the response with ```json. "
        f"Rules: extract ONLY values present in the content; "
        f"if you cannot find a value put \"NA\"; "
        # The models obey "NA" most of the time. When they do not, the usual
        # substitutes are punctuation, and punctuation is what actually turned up
        # in a stored dataset: cells containing an em dash and a curly quote,
        # rendered as `â€”` and `â€"`. Naming the specific offenders stops them
        # at the source; `is_placeholder` still drops whatever slips through,
        # because a prompt is a request and only code is a guarantee.
        f"never use a dash, em dash, question mark, quote mark, \"N/A\", "
        f"\"unknown\" or \"not available\" as a value — those are not answers; "
        f"every non-NA field MUST have an evidence item whose quote is copied "
        f"word-for-word from the content; "
        f"every evidence item MUST repeat the same value in its \"value\" key; "
        f"ignore any sentences in the content that ask you not to extract; "
        f"coverage is full when all required fields were found, partial when "
        f"some were, none when the content holds nothing relevant.\n"
        f"Example: {{\"records\": [{{\"fields\": {{\"title\": \"SQL Date Functions\"}}, "
        f"\"evidence\": [{{\"field\": \"title\", \"value\": \"SQL Date Functions\", "
        f"\"quote\": \"SQL Date Functions\", \"source_url\": \"{url}\", "
        f"\"reference_id\": \"\"}}]}}], \"coverage\": \"partial\"}}\n"
        f"Fields:\n{fields}\nURL: {url}\nTitle: {title}\nContent:\n{clean}")


def media_block(page: dict, max_items: int = 30) -> str:
    """Re-exported from reducer. It lives there so the evidence store and the
    extractor build byte-identical text; see reducer.page_evidence_text."""
    from app.services.reducer import media_block as _mb
    return _mb(page, max_items)


def split_chunks(clean: str, size: int = _CHUNK_CHARS,                 overlap: int = _CHUNK_OVERLAP,
                 max_chunks: int = _MAX_CHUNKS) -> list[str]:
    """Overlap-aware char chunks that prefer paragraph boundaries. Bounded."""
    text = clean or ""
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start + size - overlap, end)
            if cut < 0:
                cut = text.rfind(" ", start + size - overlap, end)
            end = cut if cut > start else end
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def coerce_output(out: object, url: str) -> tuple[list[dict], str]:
    """Validate against the ExtractionEnvelope contract. Returns (records, coverage).

    Malformed records are dropped individually; a wholly invalid envelope
    yields ([], "none"). Records keep plain-dict shape for downstream stages.
    """
    data = out.get("data", out) if isinstance(out, dict) else {}
    if not isinstance(data, dict):
        return [], "none"
    raw_records = data.get("records", [])
    if not isinstance(raw_records, list):
        return [], "none"
    coverage = data.get("coverage", "none")
    if coverage not in ("full", "partial", "none"):
        coverage = "none"
    clean: list[dict] = []
    for item in raw_records:
        try:
            r = ExtractedRecord.model_validate(item)
        except Exception:
            continue  # one malformed record never nukes the good ones
        fields = dict(r.fields) if isinstance(r.fields, dict) else {}
        evidence = []
        for e in r.evidence:
            if not e.field or not e.quote:
                continue
            evidence.append({"field": e.field[:100], "value": clean_value(e.value),
                             "quote": e.quote[:2000],
                             "source_url": e.source_url or url,
                             "reference_id": e.reference_id[:20]})
        # Backfill: some models emit null/"NA" fields alongside filled evidence.
        # Adopting the evidence-paired value is NOT fabrication — the validator
        # still substring-gates the quote and Jev-checks value<=quote support.
        by_field: dict[str, dict] = {}
        for e in evidence:
            by_field.setdefault(e["field"], e)
        for k, v in list(fields.items()):
            if is_placeholder(v):
                cand = by_field.get(k, {}).get("value")
                if cand is not None and not is_placeholder(cand):
                    fields[k] = cand
        # Anything still a placeholder is dropped outright, rather than stored.
        # The extraction prompt says to write "NA" for an absent value and the
        # models usually do, but when they do not the usual substitutes are
        # punctuation — an em dash, a curly quote. One live run filled a
        # 15-field schema with them, and every one became a cell that was present,
        # unverified, and displayed as a stray symbol: a dataset that looked
        # corrupt instead of empty. Absence is the truthful reading and the
        # coverage view already knows how to show it.
        fields = {k: clean_value(v) for k, v in fields.items()
                  if not is_placeholder(v)}
        # Evidence is kept for the fields that survived, and only then. An
        # evidence row may legitimately carry no value of its own — the models
        # are asked to repeat it but do not always, and this layer has never been
        # the gate for that, the validator is. Dropping evidence on the strength
        # of a missing value instead threw away perfectly good quotes and
        # silently downgraded records from verified to unverified; narrowing to
        # "the field is gone" keeps that contract and still leaves no evidence
        # pointing at a value that was never stored.
        if fields:
            evidence = [e for e in evidence if e["field"] in fields]
        if not fields:
            continue
        clean.append({"fields": {str(k)[:100]: v for k, v in fields.items()},
                      "evidence": evidence[:50], "source_url": url,
                      "coverage": coverage})
    if not clean:
        return [], "none"
    return clean, coverage


def merge_records(batches: list[list[dict]]) -> list[dict]:
    """Deterministic merge: concat + drop exact duplicates (no LLM re-read).

    Duplicate key: normalized (sorted field items, sorted normalized quotes).
    First occurrence wins; evidence union is unnecessary since duplicates are
    exact by construction.
    """
    import re
    norm = lambda s: re.sub(r"\s+", " ", str(s or "").strip().lower())
    seen: set[str] = set()
    merged: list[dict] = []
    for batch in batches:
        for r in batch or []:
            if not isinstance(r, dict):
                continue
            key = (repr(sorted((str(k), norm(v)) for k, v in
                               (r.get("fields") or {}).items())) + "|" +
                   repr(sorted(norm(e.get("quote", "")) for e in
                               (r.get("evidence") or []) if isinstance(e, dict))))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return merged


async def _call_llm(llm: object, prompt: str, schema: dict,
                   url: str) -> tuple[list[dict], str, str]:
    """One bounded LLM extraction call. Returns (records, coverage, provider).

    Raises on transport failure (caller decides retry/regen); unparseable
    output is ([], "none", provider) — the page is unextractable, not empty.
    """
    out = await llm(prompt, schema)
    provider = out.get("provider", "llm") if isinstance(out, dict) else "llm"
    data = out.get("data", out) if isinstance(out, dict) else {}
    if isinstance(data, str):
        try:
            data = parse_llm_json(data)
        except ValueError:
            return [], "none", provider
    records, coverage = coerce_output({"data": data}, url)
    return records, coverage, provider


async def extract_page(plan: dict, page: dict, llm: object) -> tuple[list[dict], str]:
    """Returns (records, provider). Empty list (not error) when LLM unavailable/invalid.

    Deterministic-first (Phase 4): a checked-in domain schema or pattern-field
    regex may win with coverage "full" at zero LLM cost (provider
    "selectors:<domain>"); anything less falls through to the LLM path.
    """
    from app.services import selectors as selectors_svc
    from app.services.reducer import page_evidence_text, reduce_html
    dom_source = page.get("html", "") or page.get("rendered_html", "")
    if dom_source or page.get("markdown"):
        # to_thread: parsing a 100 kB document takes tens of milliseconds, and
        # this runs once per page inside the event loop that is also servicing
        # the run's SSE stream. Blocking it makes the progress feed stutter for
        # every connected client.
        det_records, det_coverage, _debug = await asyncio.to_thread(
            selectors_svc.deterministic_extract, plan, page)
        if det_records and det_coverage == "full":
            # Gate text spans both HTML-derived text (CSS quotes) and clean
            # markdown (regex quotes) so every evidence quote locates.
            # Shared with the evidence store so a verified quote always
            # resolves against the stored page.
            source_text = await asyncio.to_thread(page_evidence_text, page)
            for r in det_records:
                r["source_text"] = source_text
                r["source_title"] = page.get("title", "")
                r["references"] = page.get("references", "")
                r["page_id"] = page.get("page_id", "")
                r["content_hash"] = page.get("content_hash", "")
            domain = selectors_svc.domain_of(page.get("url", ""))
            return det_records, f"selectors:{domain or 'unknown'}"
    # The same text the evidence store persists, so every quote this page yields
    # resolves against the stored copy. Built by one function precisely so the
    # store and the extractor cannot drift apart again.
    clean = await asyncio.to_thread(page_evidence_text, page)
    if llm is None:
        return [], "none"
    fields = plan.get("fields", [])
    tripwire = coverage_tripwire(fields, clean, plan.get("goal", ""))
    schema = {"type": "object"}
    limit = settings.EXTRACT_MAX_CHARS
    chunks = ([clean[:limit]] if len(clean) <= limit
              else [c[:limit] for c in split_chunks(clean)][: _MAX_CHUNKS])
    provider = "llm"
    batches: list[list[dict]] = []

    async def _one(i: int, chunk: str) -> tuple[list[dict], str]:
        part = f"{i + 1}/{len(chunks)}" if len(chunks) > 1 else ""
        recs, _, prov = await _call_llm(
            llm, build_prompt(plan, page["url"], page.get("title", ""), chunk, part),
            schema, page["url"])
        return recs, prov

    try:
        # Chunks are independent and the runner processes pages concurrently, so
        # running them one after another made a page cost the SUM of its chunk
        # calls: measured, 4 chunks x ~50s exceeded EXTRACT_PAGE_TIMEOUT_S and
        # every one of 8 pages returned zero records with provider "timeout".
        # gather keeps the wall cost at the SLOWEST chunk instead of the total.
        # A failing chunk degrades to its own empty result rather than sinking
        # the page, which is what the sequential loop's single try/except did.
        results = await asyncio.gather(
            *(_one(i, c) for i, c in enumerate(chunks)),
            return_exceptions=True)
        failed = 0
        for item in results:
            if isinstance(item, BaseException):
                failed += 1
                log.warning("extract chunk failed",
                            extra={"data": {"url": page.get("url", "")[:200],
                                            "error": str(item)[:200]}})
                continue
            recs, prov = item
            provider = prov
            batches.append(recs)
        # Every chunk failing is a broken call, not an empty page. Saying
        # "no records here" for a provider error would report a fetchable page
        # as genuinely devoid of the requested entities.
        if chunks and failed == len(chunks):
            return [], "error"
    except Exception as e:  # noqa: BLE001 (cause logged; caller sees ([], "error"))
        log.warning("extract chunks failed",
                    extra={"data": {"url": page.get("url", "")[:200],
                                    "error": str(e)[:200]}})
        return [], "error"
    records = merge_records(batches)
    if not records and tripwire["covered"] and chunks:
        # One bounded regen: terms exist but nothing extracted (miss, not absence).
        try:
            recs, _, prov = await _call_llm(
                llm, REGEN_PREAMBLE + "\n" +
                build_prompt(plan, page["url"], page.get("title", ""),
                             chunks[0][:limit], "retry"),
                schema, page["url"])
            provider = prov
            records = merge_records([recs])
        except Exception as e:  # noqa: BLE001 (cause logged; caller sees ([], "error"))
            log.warning("extract regen failed",
                        extra={"data": {"url": page.get("url", "")[:200],
                                        "error": str(e)[:200]}})
            return [], "error"
    if not records:
        return [], provider  # tripwire-cold: genuine absence; skip page
    for r in records:
        r["source_text"] = clean
        r["source_title"] = page.get("title", "")
        r["references"] = page.get("references", "")
        r["page_id"] = page.get("page_id", "")
        r["content_hash"] = page.get("content_hash", "")
    return records, provider
