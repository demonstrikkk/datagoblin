"""Phase-4 tests: deterministic-first extraction. Hermetic (no network, no LLM).

Checked-in schemas are NEVER touched: SELECTORS_DIR is monkeypatched to tmp.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.services import extractor as extractor_svc  # noqa: E402
from app.services import selectors as selectors_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


LISTING_HTML = """
<html><body>
<div class="quote"><span class="text">First thought</span>
  <small class="author">Ada</small>
  <div class="tags"><a class="tag">wisdom</a><a class="tag">code</a></div></div>
<div class="quote"><span class="text">Second thought</span>
  <small class="author">Grace</small>
  <div class="tags"><a class="tag">ships</a></div></div>
</body></html>"""

FIELDS = [{"name": "quote_text", "type": "string", "description": "Quote", "required": True},
          {"name": "author", "type": "string", "description": "Author", "required": True},
          {"name": "tags", "type": "string", "description": "Tags", "required": False}]


@pytest.fixture
def sel_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod.settings, "SELECTORS_DIR", str(tmp_path))
    return tmp_path


def _write_schema(sel_dir, domain, doc):
    (sel_dir / f"{domain}.json").write_text(json.dumps(doc), encoding="utf-8")


# -- loader ----------------------------------------------------------------------
def test_load_schema_shapes_and_rejects(sel_dir):
    assert selectors_svc.load_schema("missing.example") is None
    assert selectors_svc.load_schema("") is None
    _write_schema(sel_dir, "bad.example", {"fields": "nope"})
    assert selectors_svc.load_schema("bad.example") is None
    (sel_dir / "broken.example.json").write_text("{invalid", encoding="utf-8")
    assert selectors_svc.load_schema("broken.example") is None
    _write_schema(sel_dir, "ok.example", {
        "domain": "ok.example", "item": "div.quote",
        "fields": {"a": {"selectors": ["h1", "h2", "h3", "h4", "h5", "h6", "p"],
                          "snapshot": {"tag": "h1"}, "multiple": True},
                   "b": {"selectors": []}, "c": "nope"}})
    schema = selectors_svc.load_schema("ok.example")
    assert schema["item"] == "div.quote"
    assert schema["fields"]["a"]["selectors"] == ["h1", "h2", "h3", "h4", "h5"]
    assert schema["fields"]["a"]["multiple"] is True
    assert set(schema["fields"]) == {"a"}


def test_domain_of():
    assert selectors_svc.domain_of("https://WWW.Example.COM/a") == "www.example.com"
    assert selectors_svc.domain_of("::::") == ""


# -- CSS ---------------------------------------------------------------------------
def test_css_extract_first_hit_and_fallback():
    html = '<div><h1>Title</h1><p class="x">Para</p></div>'
    assert selectors_svc.css_extract(html, ["h2", "p.x"]) == ("Para", "p.x")
    assert selectors_svc.css_extract(html, ["h2", "h3"]) == ("", "")
    assert selectors_svc.css_extract(html, ["!!!bad", "h1"]) == ("Title", "h1")
    assert selectors_svc.css_extract("", ["h1"]) == ("", "")


# -- regex ---------------------------------------------------------------------------
def test_regex_extract_gated():
    text = "Contact jane@acme.ai or visit https://acme.ai, price $15M, call +1 (555) 123-4567."
    assert selectors_svc.regex_extract("contact_email", text)[0] == "jane@acme.ai"
    assert selectors_svc.regex_extract("website", text)[0] == "https://acme.ai,"
    assert selectors_svc.regex_extract("funding", text)[0] == "$15M"
    assert selectors_svc.regex_extract("phone", text)[0] == "+1 (555) 123-4567"
    # no hint, no fire — even though the patterns match the text
    assert selectors_svc.regex_extract("company_name", text) == ("", "")
    assert selectors_svc.regex_extract("founded", "founded 2020") == ("", "")
    assert selectors_svc.regex_extract("email", "nothing here") == ("", "")


# -- relocate --------------------------------------------------------------------------
def test_relocate_survives_rename():
    before = '<div><span class="text" itemprop="text">Hello world today</span></div>'
    after = '<div><span class="txt" itemprop="text">Hello world today</span></div>'
    from bs4 import BeautifulSoup
    el = BeautifulSoup(before, "html.parser").select_one("span.text")
    snapshot = selectors_svc._snapshot_element(el)
    text, why, top = selectors_svc.relocate(after, snapshot)
    assert (text, why) == ("Hello world today", "relocated")
    assert len(top) > 0 and top[0]["score"] >= selectors_svc.RELOCATE_THRESHOLD


def test_relocate_rejects_and_empty_snapshot():
    text, why, top = selectors_svc.relocate("<div><p>unrelated</p></div>",
                                            {"tag": "table", "attrs": {"id": "zzz"},
                                             "text": "zzz 111 222", "parent_tag": "body"})
    assert (text, why) == ("", "")
    assert isinstance(top, list)
    assert selectors_svc.relocate("<div/>", {}) == ("", "", [])


# -- deterministic_extract ---------------------------------------------------------------
def _listing_schema():
    return {"domain": "x.example", "item": "div.quote",
            "fields": {
                "quote_text": {"selectors": ["span.text"], "snapshot": {}},
                "author": {"selectors": ["small.author"], "snapshot": {}},
                "tags": {"selectors": ["a.tag"], "multiple": True, "snapshot": {}}}}


def test_deterministic_listing(sel_dir):
    _write_schema(sel_dir, "x.example", _listing_schema())
    plan = {"fields": FIELDS}
    page = {"url": "https://x.example/", "title": "Q", "html": LISTING_HTML}
    records, coverage, debug = selectors_svc.deterministic_extract(plan, page)
    assert coverage == "full" and len(records) == 2
    assert records[0]["fields"] == {"quote_text": "First thought", "author": "Ada",
                                    "tags": "wisdom | code"}
    assert records[1]["fields"]["author"] == "Grace"
    assert all(e["quote"] == e["value"] for r in records for e in r["evidence"])
    assert debug["quote_text"]["method"] == "css:span.text"


def test_deterministic_partial_and_none(sel_dir):
    _write_schema(sel_dir, "x.example", {"domain": "x.example", "fields": {
        "author": {"selectors": ["small.author"], "snapshot": {}}}})
    plan = {"fields": FIELDS}  # quote_text required but has no schema entry
    page = {"url": "https://x.example/", "title": "Q", "html": LISTING_HTML,
            "markdown": "no emails here"}
    records, coverage, _ = selectors_svc.deterministic_extract(plan, page)
    # page-level (no "item"): one record from first matches; required quote
    # missing -> partial
    assert coverage == "partial" and len(records) == 1
    assert records[0]["fields"] == {"author": "Ada"}
    assert selectors_svc.deterministic_extract({"fields": []}, page) == ([], "none", {})
    assert selectors_svc.deterministic_extract(plan, {"url": "u"}) == ([], "none", {})


def test_deterministic_regex_fallback_no_schema(sel_dir):
    plan = {"fields": [{"name": "contact_email", "type": "string",
                        "description": "Email", "required": True}]}
    page = {"url": "https://unknown.example/c", "title": "",
            "markdown": "Write to jane@acme.ai for details."}
    records, coverage, _ = selectors_svc.deterministic_extract(plan, page)
    assert coverage == "full" and records[0]["fields"] == {"contact_email": "jane@acme.ai"}


def test_page_text_strips_scripts():
    html = ("<html><head><title>T</title><script>var x = 1;</script></head>"
            "<body><p>Visible</p></body></html>")
    text = selectors_svc.page_text(html)
    assert "Visible" in text and "var x" not in text


# -- extractor hook ------------------------------------------------------------------------
def test_extract_page_deterministic_win_no_llm(sel_dir):
    _write_schema(sel_dir, "x.example", _listing_schema())

    async def _llm(prompt, schema):
        raise AssertionError("LLM must not be called on a full deterministic win")

    page = {"url": "https://x.example/", "title": "Q", "html": LISTING_HTML}
    recs, provider = run(extractor_svc.extract_page({"fields": FIELDS}, page, _llm))
    assert provider == "selectors:x.example" and len(recs) == 2
    # quotes gate against the preserved source text -> verified downstream
    wrapped = run(validator_svc.wrap_record(
        FIELDS, recs[0], recs[0]["source_text"], recs[0]["source_url"], "Q"))
    assert wrapped["quote_text"]["verification_status"] == "verified"
    assert wrapped["quote_text"]["source"]["start"] is not None


def test_extract_page_partial_falls_through_to_llm(sel_dir):
    _write_schema(sel_dir, "x.example", {"domain": "x.example", "fields": {
        "author": {"selectors": ["small.author"], "snapshot": {}}}})
    calls: list = []

    async def _llm(prompt, schema):
        calls.append(prompt)
        return {"data": {"records": [
            {"fields": {"quote_text": "First thought"},
             "evidence": [{"field": "quote_text", "quote": "First thought",
                           "source_url": "https://x.example/"}]}],
            "coverage": "partial"}, "provider": "test-llm"}

    page = {"url": "https://x.example/", "title": "Q", "html": LISTING_HTML}
    recs, provider = run(extractor_svc.extract_page({"fields": FIELDS}, page, _llm))
    assert provider == "test-llm" and len(calls) == 1 and len(recs) == 1


# -- schema-gen prompt -----------------------------------------------------------------------
def test_build_schema_prompt_shape():
    p = selectors_svc.build_schema_prompt(FIELDS, "<div>hi</div>", "https://x.example/")
    assert "selectors" in p and "quote_text" in p and "<div>hi</div>" in p
    assert "No prose" in p


def test_gen_script_proposal_validation():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_selectors",
        Path(__file__).resolve().parents[1] / "scripts" / "gen_selectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    good = mod._validate_proposal({"fields": {
        "a": {"selectors": ["h1"], "snapshot": {"tag": "h1"}}, "b": {"selectors": []}}})
    assert set(good["fields"]) == {"a"}
    with pytest.raises(ValueError):
        mod._validate_proposal({"fields": {"a": {"selectors": []}}})
    with pytest.raises(ValueError):
        mod._validate_proposal({"nope": 1})
