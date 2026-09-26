"""Phase-1 evidence-chain tests — quote-exact grounding, no network, no keys.

Covers: ExtractionEnvelope contract, tolerant JSON ladder, coverage tripwire,
quote offsets, simplified schema, single/chunk/regen extract paths, reference
gate, offset pinning in wrap_record, reducer rung ladder, and one end-to-end
page -> wrapped-record grounding assertion on saved fixtures.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.evidence import (coverage_tripwire, expected_terms,  # noqa: E402
                                  locate_quote, parse_llm_json, simplify_schema)
from app.services import extractor as extractor_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


CITED_MD = (
    "# AI Startups\n\nConcurrence⟨1⟩ builds unified clinical workflows. "
    "It was founded by Jane Doe in London.\n\n"
    "Blossom⟨2⟩ works on health insurance infrastructure.\n\n"
    "## References\n\n⟨1⟩ https://www.concurrence.com: Concurrence\n"
    "⟨2⟩ https://joinblossomhealth.com: Blossom\n"
)
CITED_REFS = "## References\n\n⟨1⟩ https://www.concurrence.com: Concurrence\n⟨2⟩ https://joinblossomhealth.com: Blossom\n"

PLAIN_MD = "Acme AI, founded by Jane Doe, raised a Series A in London."

IRRELEVANT = "Weather report: light rain over the harbor. Traffic is moderate."

FIELDS = [{"name": "company_name", "type": "string", "description": "Company name",
           "required": True},
          {"name": "founder", "type": "string", "description": "Founder name",
           "required": False}]


def _llm_factory(responses, calls=None):
    async def _llm(prompt, schema):
        (calls if calls is not None else []).append(prompt)
        item = responses[min(len(calls or []), len(responses)) - 1] \
            if calls is not None else responses[0]
        if isinstance(item, Exception):
            raise item
        return {"data": item, "provider": "test-llm"}
    return _llm


# -- contract -----------------------------------------------------------------
def test_envelope_accepts_good_drops_malformed():
    out = {"data": {"records": [
        {"fields": {"company_name": "Acme"},
         "evidence": [{"field": "company_name", "quote": "Acme",
                       "source_url": "https://x.example/a"}]},
        {"fields": {}, "evidence": []},  # no fields: dropped
        {"fields": {"company_name": "NoEv"}, "evidence": [{"field": "", "quote": ""}]},
        "not-a-record",
    ], "coverage": "partial"}}
    recs, coverage = extractor_svc.coerce_output(out, "https://x.example/a")
    assert coverage == "partial"
    # Acme kept with evidence; NoEv kept with EMPTY evidence (the validator,
    # not the coercer, is the gate — it will mark NoEv unverified downstream).
    assert [r["fields"]["company_name"] for r in recs] == ["Acme", "NoEv"]
    assert recs[0]["evidence"][0]["source_url"] == "https://x.example/a"
    assert recs[1]["evidence"] == []


def test_envelope_invalid_yields_none():
    assert extractor_svc.coerce_output({"data": "just a string"}, "u") == ([], "none")
    assert extractor_svc.coerce_output({"data": {"records": "nope"}}, "u") == ([], "none")


def test_coerce_backfills_null_fields_from_evidence():
    out = {"data": {"records": [
        {"fields": {"company_name": None, "founder": "NA", "location": "Nowhere"},
         "evidence": [
             {"field": "company_name", "value": "Acme AI", "quote": "Acme AI",
              "source_url": "u"},
             {"field": "founder", "value": "  ", "quote": "Jane",
              "source_url": "u"}]},
    ], "coverage": "partial"}}
    recs, _ = extractor_svc.coerce_output(out, "u")
    assert recs[0]["fields"]["company_name"] == "Acme AI"  # adopted from evidence
    assert recs[0]["fields"]["founder"] == "NA"  # blank evidence value: stays
    assert recs[0]["fields"]["location"] == "Nowhere"  # real value untouched


# -- tolerant JSON -------------------------------------------------------------
def test_parse_llm_json_ladder():
    assert parse_llm_json('{"records": []}') == {"records": []}
    assert parse_llm_json('```json\n{"records": []}\n```') == {"records": []}
    assert parse_llm_json('{{"records": []}}') == {"records": []}
    with pytest.raises(ValueError):
        parse_llm_json("Sorry, I could not find anything.")


# -- tripwire ------------------------------------------------------------------
def test_tripwire_hot_and_cold():
    hot = coverage_tripwire(FIELDS, PLAIN_MD, "AI startups London")
    assert hot["covered"] and "london" in hot["matched"]
    cold = coverage_tripwire(FIELDS, IRRELEVANT, "AI startups London")
    assert cold["covered"] is False
    assert expected_terms(FIELDS, "") != []


# -- offsets -------------------------------------------------------------------
def test_locate_quote():
    assert locate_quote("Acme AI", "Acme AI raises seed") == (0, 7)
    assert locate_quote("acme ai", "ACME AI raises seed") == (0, 7)  # case-insensitive
    assert locate_quote("missing", "Acme AI raises seed") == (None, None)
    assert locate_quote("", "text") == (None, None)


def test_simplify_schema():
    simple = simplify_schema(FIELDS)
    assert simple["company_name"] == {"type": "string", "description": "Company name",
                                      "required": True}
    assert simplify_schema([{"no": "name"}]) == {}


# -- extract paths ---------------------------------------------------------------
def test_single_path_extract_and_metadata():
    calls: list = []
    llm = _llm_factory([{"records": [
        {"fields": {"company_name": "Acme AI"},
         "evidence": [{"field": "company_name", "quote": "Acme AI",
                       "source_url": "https://x.example/a"}]}],
        "coverage": "partial"}], calls)
    page = {"url": "https://x.example/a", "title": "T", "markdown": PLAIN_MD,
            "references": ""}
    recs, provider = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "AI startups"}, page, llm))
    assert provider == "test-llm" and len(recs) == 1 and len(calls) == 1
    assert recs[0]["source_text"] == PLAIN_MD
    assert recs[0]["references"] == "" and recs[0]["coverage"] == "partial"


def test_chunk_path_merges_and_dedupes(monkeypatch):
    from app.core import config as config_mod
    monkeypatch.setattr(config_mod.settings, "EXTRACT_MAX_CHARS", 2000)
    clean = (PLAIN_MD + " ") * 600  # ~30k chars -> multiple chunks
    seen: list = []

    async def _llm(prompt, schema):
        seen.append(prompt)
        tag = "Acme" if "Part 1/" in prompt else "Nova"
        return {"data": {"records": [
            {"fields": {"company_name": tag},
             "evidence": [{"field": "company_name", "quote": tag,
                           "source_url": "https://x.example/big"}]},
            {"fields": {"company_name": "Acme"},  # exact duplicate: merged away
             "evidence": [{"field": "company_name", "quote": "Acme",
                           "source_url": "https://x.example/big"}]}], "coverage": "partial"},
                "provider": "test-llm"}

    page = {"url": "https://x.example/big", "title": "T", "markdown": clean}
    recs, _ = run(extractor_svc.extract_page({"fields": FIELDS, "goal": ""}, page, _llm))
    assert len(seen) > 1  # chunk path: more than one LLM call
    names = sorted(r["fields"]["company_name"] for r in recs)
    assert names == ["Acme", "Nova"]


def test_regen_fires_once_when_tripwire_hot():
    calls: list = []
    llm = _llm_factory([{"records": [], "coverage": "none"},
                        {"records": [
                            {"fields": {"company_name": "Acme AI"},
                             "evidence": [{"field": "company_name", "quote": "Acme AI",
                                           "source_url": "https://x.example/a"}]}],
                         "coverage": "partial"}], calls)
    page = {"url": "https://x.example/a", "title": "T", "markdown": PLAIN_MD}
    recs, _ = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "London startups"}, page, llm))
    assert len(calls) == 2 and "previous attempt" in calls[1]
    assert recs and recs[0]["fields"]["company_name"] == "Acme AI"


def test_no_regen_when_tripwire_cold():
    calls: list = []
    llm = _llm_factory([{"records": [], "coverage": "none"}], calls)
    page = {"url": "https://x.example/a", "title": "T", "markdown": IRRELEVANT}
    recs, provider = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "AI startups London founders"}, page, llm))
    assert recs == [] and len(calls) == 1 and provider == "test-llm"


def test_extract_llm_absent_and_error():
    page = {"url": "u", "title": "", "markdown": PLAIN_MD}
    assert run(extractor_svc.extract_page({"fields": FIELDS}, page, None)) == ([], "none")

    async def _boom(prompt, schema):
        raise RuntimeError("transport down")

    assert run(extractor_svc.extract_page({"fields": FIELDS}, page, _boom)) == ([], "error")


def test_prompt_declares_evidence_contract():
    p = extractor_svc.build_prompt({"fields": FIELDS}, "https://x.example/a", "T", "body")
    for token in ("verbatim substring", '"NA"', "reference_id", "No prose",
                  "REQUIRED", "coverage", "Example", '"value" key'):
        assert token in p


def test_media_block_appended_to_clean():
    assert extractor_svc.media_block({"url": "u"}) == ""
    block = extractor_svc.media_block({
        "images": [{"src": "https://x.example/i.png", "alt": "Founder portrait"}],
        "videos": ["https://x.example/v.mp4"]})
    assert "Founder portrait" in block and "https://x.example/i.png" in block
    assert "https://x.example/v.mp4" in block

    seen: list = []

    async def _llm(prompt, schema):
        seen.append(prompt)
        return {"data": {"records": [], "coverage": "none"}, "provider": "test-llm"}

    page = {"url": "https://x.example/a", "title": "T", "markdown": "Acme AI.",
            "images": [{"src": "https://x.example/i.png", "alt": "Team photo"}]}
    recs, _ = run(extractor_svc.extract_page({"fields": FIELDS, "goal": ""}, page, _llm))
    assert recs == [] and len(seen) == 1  # tripwire-cold: single call, no regen
    assert "Team photo" in seen[0]  # alt text reaches the model


def test_structured_appendix_reaches_prompt():
    seen: list = []

    async def _llm(prompt, schema):
        seen.append(prompt)
        return {"data": {"records": [], "coverage": "none"}, "provider": "test-llm"}

    html = ('<html><head><meta name="author" content="Ada Lovelace"></head>'
            "<body><p>Body copy here, nothing about the byline.</p></body></html>")
    page = {"url": "https://x.example/a", "title": "T", "html": html}
    run(extractor_svc.extract_page(
        {"fields": [{"name": "topic", "type": "string", "description": "Topic",
                     "required": False}], "goal": ""}, page, _llm))
    assert any("Ada Lovelace" in p for p in seen)  # meta author in context


def test_rendered_snapshot_fallback_and_selectors():
    seen: list = []

    async def _llm(prompt, schema):
        seen.append(prompt)
        return {"data": {"records": [], "coverage": "none"}, "provider": "test-llm"}

    html = ("<html><body><h1 class='headline'>Snapshot Headline Here</h1></body></html>")
    page = {"url": "https://snap.example/a", "title": "T", "rendered_html": html}
    recs, _ = run(extractor_svc.extract_page({"fields": FIELDS, "goal": ""}, page, _llm))
    assert recs == [] and len(seen) == 1
    assert "Snapshot Headline Here" in seen[0]  # rendered DOM feeds the model


# -- reference gate + offsets ------------------------------------------------------
def test_reference_gate():
    ok = run(validator_svc.verify_field("Concurrence", "Concurrence",
                                        "Concurrence⟨1⟩ builds", True, "string",
                                        "⟨1⟩", CITED_REFS))
    assert ok == ("Concurrence", "verified")
    bad = run(validator_svc.verify_field("Concurrence", "Concurrence",
                                         "Concurrence⟨1⟩ builds", True, "string",
                                         "⟨9⟩", CITED_REFS))
    assert bad == (None, "unverified")


def test_wrap_record_pins_offsets_and_reference():
    raw = {"fields": {"company_name": "Concurrence", "founder": "Jane Doe"},
           "evidence": [{"field": "company_name", "quote": "Concurrence",
                         "source_url": "https://x.example/c",
                         "reference_id": "⟨1⟩"},
                        {"field": "founder", "quote": "Jane Doe",
                         "source_url": "https://x.example/c",
                         "reference_id": ""}]}
    wrapped = run(validator_svc.wrap_record(
        FIELDS, raw, CITED_MD, "https://x.example/c", "T", references=CITED_REFS))
    cell = wrapped["company_name"]
    assert cell["verification_status"] == "verified"
    assert cell["source"]["reference_id"] == "⟨1⟩"
    start, end = cell["source"]["start"], cell["source"]["end"]
    assert isinstance(start, int) and CITED_MD[start:end] == "Concurrence"
    assert wrapped["founder"]["verification_status"] == "verified"


# -- reducer ladder ------------------------------------------------------------------
def test_reducer_rungs():
    html = "<html><head><title>Acme</title></head><body><p>Acme AI raises seed.</p></body></html>"
    text = reducer_svc.reduce_html(html)
    assert "Acme AI raises seed" in text
    _, rung = reducer_svc.reduce_html_with_rung(html)
    assert rung in ("trafilatura", "bs4", "cleanup", "html2text", "script-json")


def test_reducer_script_json_recovery():
    html = ('<html><head><script id="__NEXT_DATA__" type="application/json">'
            '{"company": {"name": "Acme AI", "city": "London"}}'
            "</script></head><body></body></html>")
    text, rung = reducer_svc.reduce_html_with_rung(html)
    assert "Acme AI" in text and rung == "script-json"


# -- end-to-end grounding ---------------------------------------------------------------
def test_page_to_wrapped_record_grounding():
    llm = _llm_factory([{"records": [
        {"fields": {"company_name": "Concurrence", "founder": "Jane Doe"},
         "evidence": [
             {"field": "company_name", "quote": "Concurrence",
              "source_url": "https://x.example/c", "reference_id": "⟨1⟩"},
             {"field": "founder", "quote": "Jane Doe",
              "source_url": "https://x.example/c", "reference_id": ""}]}],
        "coverage": "full"}])
    page = {"url": "https://x.example/c", "title": "AI Startups",
            "markdown": CITED_MD, "references": CITED_REFS}
    plan = {"fields": FIELDS, "goal": "AI startups London founders"}
    recs, _ = run(extractor_svc.extract_page(plan, page, llm))
    assert len(recs) == 1
    wrapped = run(validator_svc.wrap_record(
        FIELDS, recs[0], recs[0]["source_text"], recs[0]["source_url"], "AI Startups",
        references=recs[0]["references"]))
    for name in ("company_name", "founder"):
        cell = wrapped[name]
        assert cell["verification_status"] == "verified", name
        s, e = cell["source"]["start"], cell["source"]["end"]
        assert CITED_MD[s:e] == cell["source"]["quote"], name
