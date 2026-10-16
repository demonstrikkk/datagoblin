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
from app.services.dataset_agent import _numeric, tool_records_where  # noqa: E402


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


# --- the filter tool the menu was missing -----------------------------------

_RECS = [
    {"record_id": "r1", "fields": {
        "founders": {"value": "Noah Goodman", "verification_status": "verified"},
        "funding_amount": {"value": "$1.1b"}, "name": {"value": "Humans&"}}},
    {"record_id": "r2", "fields": {
        "founders": {"value": "NA", "verification_status": "unverified"},
        "funding_amount": {"value": "$200k"}, "name": {"value": "Tinyco"}}},
    {"record_id": "r3", "fields": {
        "founders": {}, "funding_amount": {"value": "3 million euro"},
        "name": {"value": "Mid"}}},
    {"record_id": "r4", "fields": {
        "founders": {"value": "Jane"}, "funding_amount": {"value": "Series A undisclosed"},
        "name": {"value": "X"}}},
]

_CTX = {"records": _RECS, "fields": ["founders", "funding_amount", "name"]}


@pytest.mark.parametrize("text,expected", [
    ("$1.1b", 1.1e9),
    ("1,100,000,000", 1.1e9),
    ("USD 2.5 million", 2.5e6),
    ("3 million euro", 3e6),
    ("12.5%", 12.5),
    ("42", 42.0),
    ("n/a", None),
    ("", None),
    ("unknown", None),
])
def test_numeric_reads_the_formats_stored_values_actually_use(text, expected):
    assert _numeric(text) == expected


def test_filled_uses_the_products_own_placeholder_rule():
    """A cell holding the literal `NA` is not filled. Treating it as filled
    would answer "companies whose founders column is filled" with 36 rows that
    carry no founder at all."""
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "founders", "op": "filled"}], "limit": 5})
    assert out["matched"] == 2, [r["record_id"] for r in out["records"]]
    assert {r["record_id"] for r in out["records"]} == {"r1", "r4"}


def test_empty_is_the_complement_of_filled():
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "founders", "op": "empty"}], "limit": 5})
    assert {r["record_id"] for r in out["records"]} == {"r2", "r3"}


def test_a_numeric_filter_works_over_currency_text():
    """The question that was actually asked: founders where the funding is
    above a million dollars."""
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1000000"}],
        "limit": 5})
    assert out["matched"] == 2
    assert {r["record_id"] for r in out["records"]} == {"r1", "r3"}


def test_unreadable_values_are_reported_not_treated_as_zero():
    """A comparator that silently reads a non-number as 0 would match every row
    against a million-dollar threshold. "Series A undisclosed" is a real value
    that is not a number, so it must be reported rather than skipped quietly."""
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1"}], "limit": 5})
    assert out.get("unreadable", {}).get("funding_amount") == 1
    assert "undercounts" in out["note"]
    assert "r4" not in {r["record_id"] for r in out["records"]}


def test_a_placeholder_is_not_counted_as_unreadable():
    """`unknown` is a placeholder in this product's own vocabulary, so a cell
    holding it is absent — not a value that failed to parse. Counting it as
    unreadable overstates how much of the dataset could not be read."""
    from app.services import normalizer as norm
    assert norm.is_placeholder("unknown")
    ctx = {"records": [{"record_id": "r7", "fields": {
        "funding_amount": {"value": "unknown"}}}], "fields": ["funding_amount"]}
    out = tool_records_where(ctx, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1"}], "limit": 5})
    assert out["matched"] == 0
    assert "unreadable" not in out


def test_conditions_combine_as_and():
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "founders", "op": "filled"},
        {"field": "funding_amount", "op": "gt", "value": "1000000"}],
        "limit": 5})
    assert {r["record_id"] for r in out["records"]} == {"r1"}


def test_the_filter_counts_every_match_even_when_the_list_is_capped():
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "founders", "op": "filled"}], "limit": 1})
    assert out["matched"] == 2 and out["shown"] == 1 and out["truncated"] is True


@pytest.mark.parametrize("bad", [
    {}, {"conditions": []}, {"conditions": "founders"},
    {"conditions": [{"field": "nope", "op": "filled"}]},
    {"conditions": [{"field": "founders", "op": "between"}]},
    {"conditions": [{"field": "funding_amount", "op": "gt", "value": "lots"}]},
])
def test_a_bad_filter_is_refused_with_a_reason(bad):
    with pytest.raises(Exception) as exc:
        tool_records_where(_CTX, bad)
    assert str(exc.value), "a refusal must say what was wrong"


def test_an_empty_cell_is_absent_not_unreadable():
    """A live answer reported "7 values unreadable" when those 7 records simply
    carried no figure at all. Absent is not unparseable, and saying otherwise
    understates how much of the dataset was actually read."""
    ctx = {"records": [{"record_id": "r5", "fields": {"funding_amount": {}}}],
           "fields": ["funding_amount"]}
    out = tool_records_where(ctx, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1"}], "limit": 5})
    assert out["matched"] == 0
    assert "unreadable" not in out


def test_a_non_numeric_value_is_still_reported():
    ctx = {"records": [{"record_id": "r6", "fields": {
        "funding_amount": {"value": "Series A undisclosed"}}}],
        "fields": ["funding_amount"]}
    out = tool_records_where(ctx, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1"}], "limit": 5})
    assert out["unreadable"] == {"funding_amount": 1}


def test_mixing_currencies_is_flagged_rather_than_silently_converted():
    """`£6m` is greater than 1000000 as a number and the dataset does not say
    what that is in dollars. The comparison is honest arithmetic, but it must
    not read as a dollar figure."""
    ctx = {"records": [
        {"record_id": "g1", "fields": {"funding_amount": {"value": "£6m"}}},
        {"record_id": "g2", "fields": {"funding_amount": {"value": "€3 million"}}},
        {"record_id": "g3", "fields": {"funding_amount": {"value": "$1.1b"}}}],
        "fields": ["funding_amount"]}
    out = tool_records_where(ctx, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1000000"}], "limit": 5})
    assert out["matched"] == 3
    assert out["currencies"] == ["£", "€"]
    assert "not converted" in out["note"]


def test_a_dollar_only_comparison_reports_no_currency_caveat():
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1000000"}],
        "limit": 5})
    assert "currencies" not in out


def test_records_where_summarises_readably():
    from app.services import dataset_agent as agent_svc
    out = tool_records_where(_CTX, {"conditions": [
        {"field": "founders", "op": "filled"}], "limit": 5})
    line = agent_svc._summarise("records_where", out)
    assert "2 records match" in line and "founders filled" in line
    # An unreadable count must reach the trace, not just the payload.
    out2 = tool_records_where(_CTX, {"conditions": [
        {"field": "funding_amount", "op": "gt", "value": "1"}], "limit": 5})
    assert "unreadable" in agent_svc._summarise("records_where", out2)


def test_the_tool_is_offered_to_the_model():
    from app.services import dataset_agent as agent_svc
    assert "records_where" in agent_svc.TOOLS
    menu = agent_svc._TOOL_MENU
    assert "records_where" in menu
    for op in ("filled", "empty", "equals", "contains", "gt", "lt"):
        assert op in menu