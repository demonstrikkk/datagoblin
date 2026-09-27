"""Records must cite a STORED page, and a verified quote must resolve against it.

The pipeline used to reduce a page in RAM, extract from it and throw it away,
so `start`/`end` offsets pointed into text that no longer existed and
`content_hash` could never be re-checked. This asserts the whole chain holds:
page stored -> id returned -> carried on the record -> written into provenance
-> and the quote still locates in the text that was actually stored.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _noop(_):
    pass


HTML = ("<html><body><h1>Acme Corp</h1>"
        "<p>Acme Corp was founded in 2011 and employs roughly 240 people.</p>"
        "</body></html>")


def _page(url="https://x.example/a"):
    return {"url": url, "final_url": url, "title": "Acme", "html": HTML,
            "text": "", "method": "http"}


def test_stored_text_contains_every_quotable_character():
    """Media alt-text and JSON-LD are quotable evidence, so if the store omits
    them a quote lifted from them cannot be re-verified."""
    page = _page()
    page["images"] = [{"alt": "Acme office in Berlin", "src": "https://x.example/o.jpg"}]
    page["html"] = HTML.replace("</body>",
                                '<script type="application/ld+json">'
                                '{"@type":"Organization","name":"Acme Corp"}'
                                '</script></body>')
    text = reducer_svc.page_evidence_text(page)
    assert "Acme office in Berlin" in text, "media alt-text must be quotable"
    assert "Acme Corp" in text
    assert "employs roughly 240" in text


def test_rendered_page_text_is_the_markdown():
    page = {"url": "https://x.example/a", "method": "crawl4ai",
            "markdown": "# Acme\n\nAcme Corp employs 240 people.",
            "rendered_html": "<html><body><div id=root></div></body></html>"}
    text = reducer_svc.page_evidence_text(page)
    assert "employs 240 people" in text


def test_page_id_reaches_provenance_and_the_quote_still_locates():
    """The end-to-end claim: a verified field names a stored page, and its
    offsets address text that is still retrievable from that page."""
    stored: dict = {}

    async def _persist_page(page):
        stored[page["url"]] = page
        return "stored-page-uuid"

    async def _fetch(url, method):
        return _page(url)

    pages, _ = run(crawler_svc.fetch_all(
        [{"url": "https://x.example/a", "title": ""}], _fetch, _noop, _noop, {},
        _persist_page))
    assert len(pages) == 1
    page = pages[0]
    assert page["page_id"] == "stored-page-uuid"

    source_text = reducer_svc.page_evidence_text(page)
    quote = "Acme Corp was founded in 2011 and employs roughly 240 people."
    fields = [{"name": "headcount", "type": "number", "required": True}]
    raw = {"fields": {"headcount": 240},
           "evidence": [{"field": "headcount", "value": 240, "quote": quote}]}

    wrapped = run(validator_svc.wrap_record(
        fields, raw, source_text, page["url"], page["title"],
        references=page.get("references", ""), page_id=page["page_id"]))

    src = wrapped["headcount"]["source"]
    assert src["page_id"] == "stored-page-uuid", "provenance must name the page"
    # The offsets must resolve inside what was actually stored. This is the
    # assertion that was impossible before pages were persisted at all.
    start, end = src["start"], src["end"]
    assert start is not None and end is not None, "verified field must be pinned"
    persisted = stored["https://x.example/a"]["markdown"]
    assert persisted[start:end] == quote, (
        "the stored page text must contain the quote at the cited offsets")


def test_page_id_survives_schema_validation():
    """SourceRef is a strict model; an undeclared page_id is silently dropped,
    which would leave provenance pointing at nothing."""
    from app.schemas.plan import ProvenanceField
    f = ProvenanceField.model_validate(
        {"value": 1, "verification_status": "verified",
         "source": {"url": "u", "quote": "q", "retrieved_at": "t",
                    "page_id": "p1", "content_hash": "h1"}})
    assert f.source.page_id == "p1"
    assert f.source.content_hash == "h1"


def test_probe_without_a_store_yields_empty_page_id_not_a_crash():
    page = _page()
    wrapped = run(validator_svc.wrap_record(
        [{"name": "x", "type": "string", "required": False}],
        {"fields": {"x": "v"}, "evidence": []},
        reducer_svc.page_evidence_text(page), page["url"], page["title"]))
    assert wrapped["x"]["source"]["page_id"] == ""
