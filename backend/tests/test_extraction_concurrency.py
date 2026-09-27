"""Per-page work must cost the SLOWEST call, not the sum of them.

Measured live, this was the difference between a usable run and an empty one:
8 pages were fetched and stored, then every single extraction returned zero
records with provider "timeout", because a page was split into up to 4 chunks
that were called one after another. The 35 conflicting cells left by dedupe were
then adjudicated one at a time, which spent the rest of the 600s budget.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.services import deduper as deduper_svc  # noqa: E402
from app.services import extractor as extractor_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


PAGE = {"url": "https://x.example/a", "title": "T", "method": "http",
        "html": "<html><body><p>Acme Corp is a European AI company.</p></body></html>"}

FIELDS = [{"name": "company_name", "type": "string", "description": "Name",
           "required": True}]


def test_chunks_are_extracted_concurrently(monkeypatch):
    """4 chunks at 50ms each must cost ~50ms, not ~200ms."""
    monkeypatch.setattr(config_mod.settings, "EXTRACT_MAX_CHARS", 400)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    in_flight = {"now": 0, "max": 0}

    async def _llm(prompt, schema):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        return {"data": {"records": [], "coverage": "none"}, "provider": "test"}

    big = "<html><body>" + ("<p>Acme AI company text here. " * 400) + "</body></html>"
    page = dict(PAGE, html=big)
    recs, provider = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "acme"}, page, _llm))
    # Peak concurrency is the invariant. Wall-clock is not asserted here because
    # it is dominated by HTML reduction, not by the calls being timed.
    assert in_flight["max"] > 1, "chunks ran one after another, not concurrently"


def test_one_failing_chunk_does_not_sink_the_page(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "EXTRACT_MAX_CHARS", 400)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    calls = {"n": 0}

    async def _llm(prompt, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chunk 1 exploded")
        return {"data": {"records": [
            {"fields": {"company_name": "Acme Corp"},
             "evidence": [{"field": "company_name",
                           "quote": "Acme Corp is a European AI company"}]}],
            "coverage": "full"}, "provider": "test"}

    big = "<html><body>" + ("<p>Acme AI company text here. " * 400) + "</body></html>"
    recs, provider = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "acme"}, dict(PAGE, html=big), _llm))
    assert recs, "a single failed chunk must not lose the records from the others"


def test_every_chunk_failing_is_an_error_not_an_empty_page(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "EXTRACT_MAX_CHARS", 400)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)

    async def _llm(prompt, schema):
        raise RuntimeError("provider down")

    big = "<html><body>" + ("<p>Acme AI company text here. " * 400) + "</body></html>"
    recs, provider = run(extractor_svc.extract_page(
        {"fields": FIELDS, "goal": "acme"}, dict(PAGE, html=big), _llm))
    assert recs == []
    assert provider == "error", (
        "a broken provider must not be reported as a page with nothing to find")


# --- conflict adjudication --------------------------------------------------

def _conflicting(n):
    return [{"fields": {"f%d" % i: {
        "value": "a", "verification_status": "conflicting",
        "source": {"quote": "q1"},
        "rivals": [{"value": "b", "source": {"quote": "q2"}}]}}} for i in range(n)]


def test_conflicts_are_judged_concurrently(monkeypatch):
    in_flight = {"now": 0, "max": 0}

    async def _triage(field, a, b, qa="", qb=""):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.03)
        in_flight["now"] -= 1
        return {"decision": "A"}

    monkeypatch.setattr(jev, "conflict_triage", _triage)
    t0 = time.perf_counter()
    stats = run(deduper_svc.adjudicate_conflicts(_conflicting(6)))
    took = time.perf_counter() - t0
    assert stats["judged"] == 6
    assert in_flight["max"] > 1, "conflicts were adjudicated one at a time"
    assert took < 0.12, f"6 conflicts took {took:.2f}s; the sum, not the max"


def test_adopting_a_rival_actually_mutates_the_cell(monkeypatch):
    """Counting a decision is not applying it."""

    async def _triage(field, a, b, qa="", qb=""):
        return {"decision": "B"}

    monkeypatch.setattr(jev, "conflict_triage", _triage)
    rows = _conflicting(1)
    stats = run(deduper_svc.adjudicate_conflicts(rows))
    cell = rows[0]["fields"]["f0"]
    assert stats["adopted"] == 1 and stats["confirmed"] == 0
    assert cell["value"] == "b", "the rival value was not adopted"
    assert cell["verification_status"] == "verified"
    assert cell["source"] == {"quote": "q2"}


def test_insufficient_downgrades_and_nulls(monkeypatch):
    async def _triage(field, a, b, qa="", qb=""):
        return {"decision": "INSUFFICIENT"}

    monkeypatch.setattr(jev, "conflict_triage", _triage)
    rows = _conflicting(1)
    stats = run(deduper_svc.adjudicate_conflicts(rows))
    cell = rows[0]["fields"]["f0"]
    assert stats["downgraded"] == 1
    assert cell["value"] is None
    assert cell["verification_status"] == "unverified"


def test_a_failed_judge_leaves_the_conflict_standing(monkeypatch):
    """The safe outcome: a conflict is never silently resolved one way."""

    async def _triage(field, a, b, qa="", qb=""):
        raise RuntimeError("judge down")

    monkeypatch.setattr(jev, "conflict_triage", _triage)
    rows = _conflicting(2)
    stats = run(deduper_svc.adjudicate_conflicts(rows))
    assert stats["judged"] == 2 and stats["adopted"] == 0
    for r in rows:
        cell = next(iter(r["fields"].values()))
        assert cell["verification_status"] == "conflicting"
        assert cell["value"] == "a"


def test_no_conflicts_means_no_judge_calls(monkeypatch):
    called = {"n": 0}

    async def _triage(field, a, b, qa="", qb=""):
        called["n"] += 1
        return {"decision": "A"}

    monkeypatch.setattr(jev, "conflict_triage", _triage)
    stats = run(deduper_svc.adjudicate_conflicts([{"fields": {"x": {
        "value": "v", "verification_status": "verified", "source": {}}}}]))
    assert stats == {"judged": 0, "confirmed": 0, "adopted": 0, "downgraded": 0}
    assert called["n"] == 0
