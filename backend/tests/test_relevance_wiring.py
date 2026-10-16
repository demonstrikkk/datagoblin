"""The gate must be wired into extraction, not merely exist.

A service that works and is not called is the most expensive kind of dead code,
and the previous session shipped exactly that shape: the per-rung timing was
added to a dispatch no run used, and every run reported nothing. These tests
exist so that failure cannot recur silently.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings as _settings  # noqa: E402
from app.services import extractor  # noqa: E402

PLAN = {
    "goal": "Find companies willing to sponsor hackathons in New Delhi in 2026.",
    "search_queries": ["hackathon sponsors New Delhi"],
    "fields": [{"name": "company_name", "type": "string", "required": True}],
}

ON_TOPIC = {"url": "https://x.example/events/sponsorship",
            "markdown": "Hackathon sponsorship packages are available."}
OFF_TOPIC = {"url": "https://x.example/products/widget",
             "markdown": "Our widget improves team productivity across orgs."}


class _BoomLLM:
    """Any call to the model is a failure: a dropped page must cost nothing."""
    def __init__(self):
        self.calls = 0

    async def __call__(self, prompt, schema):
        self.calls += 1
        raise AssertionError("the model was called for a page the gate drops")


def _gate_on(monkeypatch):
    monkeypatch.setattr(_settings, "EXTRACT_RELEVANCE_GATE", True)


@pytest.fixture(autouse=True)
def _restore():
    before = _settings.EXTRACT_RELEVANCE_GATE
    yield
    _settings.EXTRACT_RELEVANCE_GATE = before


def test_an_off_topic_page_never_reaches_the_model(monkeypatch):
    _gate_on(monkeypatch)
    llm = _BoomLLM()
    recs, provider = asyncio.run(extractor.extract_page(PLAN, OFF_TOPIC, llm))
    assert recs == []
    assert provider == "off-topic"
    assert llm.calls == 0, "the gate dropped the page but still paid for a call"


def test_an_on_topic_page_still_proceeds(monkeypatch):
    """The gate must not block everything -- that is the silent total failure."""
    _gate_on(monkeypatch)
    seen = []

    async def llm(prompt, schema):
        seen.append(prompt)
        return {"fields": {}, "evidence": []}

    recs, provider = asyncio.run(extractor.extract_page(PLAN, ON_TOPIC, llm))
    assert provider != "off-topic", "a relevant page was dropped"
    assert seen, "a relevant page never reached extraction"


def test_the_gate_can_be_turned_off(monkeypatch):
    """It is a flag, so it must genuinely be bypassable.

    Asserted through the call count rather than an exception: the extractor
    deliberately swallows model errors into a warning, so raising is not how
    this behaviour surfaces.
    """
    monkeypatch.setattr(_settings, "EXTRACT_RELEVANCE_GATE", False)
    calls = {"n": 0}

    async def counting_llm(prompt, schema):
        calls["n"] += 1
        return {"fields": {}, "evidence": []}

    asyncio.run(extractor.extract_page(PLAN, OFF_TOPIC, counting_llm))
    assert calls["n"] > 0, "with the gate off, nothing was extracted at all"


def test_the_gate_sits_before_the_expensive_work(monkeypatch):
    """Ordering is the entire saving, so compare cost rather than code.

    Asserted as a difference, not an absolute: extraction makes a variable
    number of model calls per page, so "3 pages -> 3 calls" was never a true
    statement. What must hold is that adding off-topic pages costs nothing.
    """
    _gate_on(monkeypatch)

    def run(pages):
        calls = {"n": 0}

        async def counting_llm(prompt, schema):
            calls["n"] += 1
            return {"fields": {}, "evidence": []}

        for p in pages:
            asyncio.run(extractor.extract_page(PLAN, p, counting_llm))
        return calls["n"]

    baseline = run([ON_TOPIC] * 3)
    with_junk = run([ON_TOPIC] * 3 + [OFF_TOPIC] * 5)

    assert baseline > 0, "the baseline extracted nothing, so the test proves nothing"
    assert with_junk == baseline, \
        (f"adding 5 off-topic pages cost {with_junk - baseline} extra model "
         "calls; the gate is not running before the expensive work")


def test_an_empty_page_still_reports_none_not_off_topic(monkeypatch):
    """`none` means 'nothing to extract'; conflating it hides the reason."""
    _gate_on(monkeypatch)
    recs, provider = asyncio.run(
        extractor.extract_page(PLAN, {"url": "https://x.example/"}, _BoomLLM()))
    assert provider == "none"


def test_a_page_with_no_text_is_not_labelled_off_topic(monkeypatch):
    """Reliability beats tidiness: an unreadable page is not an irrelevant one."""
    _gate_on(monkeypatch)
    _recs, provider = asyncio.run(extractor.extract_page(
        PLAN, {"url": "https://x.example/", "markdown": ""}, _BoomLLM()))
    assert provider != "off-topic"


