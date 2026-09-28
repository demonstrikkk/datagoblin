"""Selector proposals must be proven against a real page before they are offered.

The failure this replaces: a model returns a plausible CSS string, it is
written to disk, and extraction later returns nothing for that field. Nothing
in the old path ever asked whether the selector matched.
"""
import json

import pytest

from app.services import selector_learn as sl

HTML = """
<html><body>
  <div class="company">
    <h1 class="name">Northwind Traders</h1>
    <span class="hq">Berlin, Germany</span>
    <span class="founded">2011</span>
  </div>
  <article class="blog-post-body">
    <p>Long prose that would be a terrible single field value if a selector
       matched the whole container. This paragraph exists so that matching the
       container returns clearly more text than any single extracted value
       should ever be, which is what the length guard is there to catch: a
       container looks like coverage while the field value is an article that
       nobody can use, verify against a quote, or export.</p>
  </article>
</body></html>
"""


@pytest.fixture
def fake_llm(monkeypatch):
    """Stand in for the provider, recording what it was asked."""
    calls = {}

    async def _generate(prompt, schema):
        calls["prompt"] = prompt
        return {"provider": "fake", "data": calls.get("reply", {})}

    from app.providers.llm import generate as llm_mod
    monkeypatch.setattr(llm_mod, "structured_generate", _generate)
    return calls


# -- happy path -------------------------------------------------------------
def test_a_working_selector_is_returned_with_the_text_it_actually_matched(fake_llm):
    import asyncio
    fake_llm["reply"] = {"fields": {"name": {"selectors": [".name"]}}}
    out = asyncio.run(sl.propose("https://example.com/x", [{"name": "name", "type": "string"}],
                                 html=HTML))
    assert out["usable"] is True
    assert out["fields"]["name"]["selectors"] == [".name"]
    # The sample is the point: the reviewer reads what will be extracted.
    assert out["fields"]["name"]["samples"] == {".name": "Northwind Traders"}
    assert out["html_source"] == "stored page"


def test_a_selector_matching_nothing_is_reported_not_saved(fake_llm):
    import asyncio
    fake_llm["reply"] = {"fields": {"hq": {"selectors": [".does-not-exist"]}}}
    out = asyncio.run(sl.propose("https://example.com/x", [{"name": "hq", "type": "string"}],
                                 html=HTML))
    assert out["usable"] is False
    assert "hq" not in out["fields"]
    assert out["rejected"][0]["field"] == "hq"
    assert "no proposed selector returned any text" in out["rejected"][0]["reason"]
    # Everything that was tried is named, so a reviewer can tell a bad proposal
    # from a good one aimed at the wrong element.
    assert out["rejected"][0]["selectors"] == [".does-not-exist"]


def test_a_container_selector_is_rejected_as_too_broad(fake_llm):
    """Matching a whole article is worse than matching nothing: it looks like
    coverage while the field value is a paragraph."""
    import asyncio
    fake_llm["reply"] = {"fields": {"about": {"selectors": [".blog-post-body"]}}}
    out = asyncio.run(sl.propose("https://example.com/x", [{"name": "about", "type": "string"}],
                                 html=HTML))
    assert "about" not in out["fields"]
    assert any("likely a container" in r["reason"] for r in out["rejected"])


def test_alternates_are_tried_in_order_and_a_later_one_can_win(fake_llm):
    import asyncio
    fake_llm["reply"] = {"fields": {"hq": {"selectors": [".nope", ".hq"]}}}
    out = asyncio.run(sl.propose("https://example.com/x", [{"name": "hq", "type": "string"}],
                                 html=HTML))
    assert out["fields"]["hq"]["selectors"] == [".hq"]


def test_proposal_without_fields_is_an_error_not_an_empty_schema(fake_llm):
    import asyncio
    fake_llm["reply"] = {"nope": 1}
    with pytest.raises(ValueError):
        asyncio.run(sl.propose("https://example.com/x", [{"name": "a", "type": "s"}], html=HTML))


def test_a_domain_is_required():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(sl.propose("not a url", [{"name": "a", "type": "s"}], html=HTML))


# -- the sample the model actually sees --------------------------------------
def test_a_short_document_is_passed_through_whole():
    from app.services import selectors as sel
    assert sel.html_sample(HTML) == HTML


def test_a_long_document_samples_the_middle_not_just_the_head():
    """This was the reason nothing verified.

    The first 8k of a server-rendered listing page is `<head>`, scripts, and
    nav. The repeated records the selectors must target sit far later, so the
    model was shown a document containing no data and had no way to design a
    working selector for it.
    """
    from app.services import selectors as sel
    doc = "<head>" + ("x" * 8_000) + "</head>" + ("<li>Acme</li>" * 6_000)
    sample = sel.html_sample(doc, budget=12_000)
    assert "Acme" in sample, "the sample excluded the page's actual content"
    assert len(sample) < len(doc)
    assert "characters omitted" in sample


def test_the_sample_marks_itself_as_partial():
    from app.services import selectors as sel
    prompt = sel.build_schema_prompt([{"name": "a", "type": "string"}],
                                     "<head>" + ("y" * 20_000) + "<li>Acme</li>",
                                     "https://x.example")
    assert "an excerpt" in prompt, "the model cannot tell a sample from a whole page"


def test_no_fields_requested_is_refused():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(sl.propose("https://example.com", [], html=HTML))


# -- save -------------------------------------------------------------------
def test_saving_writes_a_schema_that_the_loader_accepts(tmp_path, monkeypatch,
                                                        fake_llm):
    import asyncio
    from app.services import selectors as selectors_svc
    monkeypatch.setattr(selectors_svc, "selectors_dir", lambda: tmp_path)

    fake_llm["reply"] = {"fields": {"name": {"selectors": [".name"]},
                                    "hq": {"selectors": [".hq"]}}}
    draft = asyncio.run(sl.propose("https://example.com/x",
                                   [{"name": "name", "type": "s"}, {"name": "hq", "type": "s"}],
                                   html=HTML))
    saved = sl.save(draft)
    assert saved["fields"] == 2
    assert saved["replaced"] is False

    reloaded = selectors_svc.load_schema("example.com")
    assert reloaded is not None, "the saved schema did not survive load_schema"
    assert set(reloaded["fields"]) == {"name", "hq"}


def test_saving_over_an_existing_schema_is_refused_without_overwrite(tmp_path,
                                                                   monkeypatch, fake_llm):
    import asyncio
    from app.services import selectors as selectors_svc
    monkeypatch.setattr(selectors_svc, "selectors_dir", lambda: tmp_path)
    (tmp_path / "example.com.json").write_text(json.dumps({
        "domain": "example.com",
        "fields": {"name": {"selectors": [".good"]}},
    }), encoding="utf-8")

    fake_llm["reply"] = {"fields": {"name": {"selectors": [".name"]}}}
    draft = asyncio.run(sl.propose("https://example.com/x",
                                   [{"name": "name", "type": "s"}], html=HTML))
    with pytest.raises(ValueError, match="already has a checked-in schema"):
        sl.save(draft)
    # And the working selector is still the one on disk.
    assert selectors_svc.load_schema("example.com")["fields"]["name"]["selectors"] == [".good"]

    saved = sl.save(draft, overwrite=True)
    assert saved["replaced"] is True


def test_saving_a_draft_with_nothing_verified_is_refused(tmp_path, monkeypatch):
    from app.services import selectors as selectors_svc
    monkeypatch.setattr(selectors_svc, "selectors_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        sl.save({"domain": "x.com", "page_url": "https://x.com/a", "fields": {}})


# -- list -------------------------------------------------------------------
def test_list_flags_a_schema_that_no_longer_loads(tmp_path, monkeypatch):
    from app.services import selectors as selectors_svc
    monkeypatch.setattr(selectors_svc, "selectors_dir", lambda: tmp_path)
    (tmp_path / "good.com.json").write_text(json.dumps({
        "domain": "good.com",
        "fields": {"a": {"selectors": [".x"]}},
        "generated_by": "test",
    }), encoding="utf-8")
    (tmp_path / "broken.com.json").write_text("{ not json", encoding="utf-8")

    rows = {r["domain"]: r for r in sl.list_schemas()}
    assert rows["good.com"]["valid"] is True
    assert rows["good.com"]["field_count"] == 1
    # Present on disk, unusable in practice. Reporting only the count would
    # make a dead schema look like a working one.
    assert rows["broken.com"]["valid"] is False
    assert rows["broken.com"]["field_count"] == 0
