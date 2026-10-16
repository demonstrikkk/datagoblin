"""Asking a question must never cost the answer.

The bug, reported from the Ask tab: *"founders with company valuation above 1
million dollars"* returned a **backfill proposal** and no answer at all.

Three layers each made that possible, and each is fixed and pinned here:

1. `proposals.classify` scored a bare field noun (`valuation`) as a write
   intent. The target lists say *what* a request could be about; only a hint
   says something should *change*. No verb, no gap named, no request made.
2. `dataset_agent.answer` returned **early** on any non-query intent, so one
   misclassification replaced the whole reply with a card. The tools are
   read-only, so a stray card is always a safer outcome than a missing answer.
3. The panel rendered the answer only when `intent === 'query'`, so the same
   misclassification hid it in the browser even if the server had answered.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import proposals as proposals_svc  # noqa: E402


# --- layer 1: a field noun is not a request ---------------------------------

@pytest.mark.parametrize("sentence", [
    "founders with company valuation above 1 million dollars",
    "founders with company valuation above 1 million dollars ",
    "companies founded after 2019 in Europe",
    "valuation above 1 million dollars",
    "salary and location",
])
def test_a_filter_with_no_verb_is_a_read(sentence):
    intent, _score = proposals_svc.classify(sentence)
    assert intent == "query", f"{sentence!r} was classified as {intent!r}"


@pytest.mark.parametrize("sentence", [
    "add the missing funding column",
    "fill in the blank valuation fields",
    "capture employee count",
    "backfill founders",
])
def test_an_explicit_request_still_proposes(sentence):
    intent, _score = proposals_svc.classify(sentence)
    assert intent != "query", f"{sentence!r} should still be a change request"


def test_refine_needs_a_plan_subject_not_just_a_hedge_word():
    """`only` is how a read is phrased as much as a plan change. Without a named
    plan object there is nothing to refine."""
    assert proposals_svc.classify("show only companies above 1m")[0] == "query"
    # ... but with one it is still recognised.
    assert proposals_svc.classify("use only two sources instead of five")[0] == "refine"


def test_a_question_mark_still_wins():
    assert proposals_svc.classify("add funding?")[0] == "query"


# --- layer 2 and 3: the answer survives a misclassification -----------------

def _stub_llm():
    async def _llm(prompt, schema, **kw):
        if "plan" in prompt.lower() or "tool" in prompt.lower():
            return {"data": {"calls": [{"tool": "record_count", "args": {}}]},
                    "provider": "stub", "model": "stub"}
        return {"data": {"answer": "4 companies exceed $1M.", "figures": [],
                         "chart": None}, "provider": "stub", "model": "stub"}
    return _llm


class _Store:
    def __init__(self, did="d1"):
        self.did = did
        self.rows = [{"record_id": "r1",
                      "fields": {"company_name": {"value": "Acme",
                                                  "verification_status": "verified",
                                                  "source": {"url": "https://x.example/",
                                                             "quote": "Acme",
                                                             "retrieved_at": "2026-01-01T00:00:00Z"}}}}]

    def get_dataset_schema(self, dataset_id):
        if dataset_id != self.did:
            return None
        return [{"name": "company_name", "type": "string", "description": "Name"}]

    def get_dataset(self, dataset_id):
        if dataset_id != self.did:
            return {}
        return {"id": self.did, "run_id": "r1", "name": "t",
                "schema": self.get_dataset_schema(dataset_id), "records": self.rows,
                "counts": {}}

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        if dataset_id != self.did:
            return {}
        return {"dataset_id": dataset_id, "total": len(self.rows),
                "records": self.rows}

    def get_pages(self, run_id, limit=200):
        return []

    # `proposals.propose` recovers the plan by walking dataset -> run ->
    # workflow, so a change request needs these to build a real proposal.
    def get_run(self, run_id):
        return {"id": run_id, "workflow_id": "w1", "status": "COMPLETED",
                "partial": False, "progress": 100, "stats": {}}

    def get_plan(self, plan_id):
        return {"goal": "t", "entity": "company", "requested_count": 5,
                "max_results": 5, "search_queries": ["q"],
                "fields": [{"name": "company_name", "type": "string",
                            "description": "Name"},
                           {"name": "funding", "type": "string",
                            "description": "Funding"}],
                "seed_urls": [], "allowed_sources": [], "max_pages": 4,
                "traversal": {}, "validation_rules": [],
                "dedupe_keys": ["company_name"]}


def test_a_change_request_is_also_answered():
    """The server no longer returns early on a write intent."""
    from app.services import dataset_agent as agent_svc

    store = _Store()
    out = asyncio.run(agent_svc.answer(
        store, "d1", "add the missing funding column", _stub_llm()))
    assert out["answer"], "a change request produced no answer text"
    assert out["trace"], "no tool ran, so nothing answered the question"
    # The proposal rides along; it does not replace the reply.
    assert out["intent"] == "backfill"
    assert out["proposals"] and out["proposals"][0]["intent"] == "backfill"


def test_a_filter_is_answered_with_no_stray_proposal():
    from app.services import dataset_agent as agent_svc

    store = _Store()
    out = asyncio.run(agent_svc.answer(
        store, "d1", "founders with company valuation above 1 million dollars",
        _stub_llm()))
    assert out["answer"]
    assert out["proposals"] == []
    assert out["intent"] == "query"


def test_the_answer_never_depends_on_the_classifier():
    """The invariant, stated once: whatever the classifier believes, a question
    still gets its answer."""
    from app.services import dataset_agent as agent_svc

    for sentence in ("founders with company valuation above 1 million dollars",
                     "add the missing funding column",
                     "refresh the sources",
                     "only two sources instead of five"):
        out = asyncio.run(agent_svc.answer(_Store(), "d1", sentence, _stub_llm()))
        assert out.get("answer"), f"no answer for {sentence!r}"
        assert out.get("trace"), f"no tool ran for {sentence!r}"


def test_a_failing_proposal_still_leaves_the_answer():
    """The proposal is advisory. If it cannot be built, the reader must not lose
    the reply they asked for."""
    from app.services import dataset_agent as agent_svc

    class _Broken(_Store):
        def get_run(self, run_id):
            raise RuntimeError("run table unavailable")

    out = asyncio.run(agent_svc.answer(
        _Broken(), "d1", "add the missing funding column", _stub_llm()))
    assert out["answer"]
    assert out["proposals"] == []


def test_the_panel_does_not_gate_the_answer_on_intent():
    """Layer 3 is a frontend guard; assert the source no longer has it, so the
    gate cannot come back with a refactor."""
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "DatasetPage.jsx").read_text(encoding="utf-8")
    assert "result.intent === 'query'" not in src, (
        "the answer must render whenever there is one, not only for a "
        "classifier the backend already answered")
    assert "{result && result.answer ?" in src