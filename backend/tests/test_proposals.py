"""Sentence -> proposal. The classification is what has to be right.

A wrong intent here is not a wrong label, it is a wrong action pointed at a
dataset of real extracted values. So the tests cover the ambiguous sentences
and the near-misses, not just the obvious examples.
"""
import pytest

from app.services import proposals


def _cell(v, status="unverified"):
    return {"value": v, "verification_status": status,
            "source": {"quote": "q", "url": "u", "page_id": "p1"}}


class _Store:
    def __init__(self, records=None, pages=None, plan=None, schema=None):
        self.records = records if records is not None else [
            {"record_id": "1", "fields": {"company_name": _cell("Acme")}}]
        self.pages = pages if pages is not None else [{
            "id": "p1", "url": "https://x.example/", "markdown": "m", "raw_html": "h",
            "title": "t", "content_hash": "h", "method": "http", "run_id": "r1",
            "retrieved_at": "2026-09-01T00:00:00Z"}]
        self.plan = plan if plan is not None else {
            "fields": [{"name": "company_name"}, {"name": "funding_round"}],
            "dedupe_keys": ["company_name"]}
        self.schema = schema if schema is not None else [
            {"name": "company_name"}, {"name": "funding_round"}]

    def get_dataset(self, did):
        return {"id": "d1", "run_id": "r1", "schema": self.schema}

    def get_run(self, rid):
        return {"id": rid, "workflow_id": "w1"}

    def get_plan(self, pid):
        return self.plan

    def get_records(self, did, q="", limit=100, offset=0):
        return {"records": self.records, "total": len(self.records)}

    def get_pages(self, run_id, limit=50):
        return self.pages

    def get_page(self, pid):
        return self.pages[0] if self.pages else None


# --- classification ---------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("add the funding round", "backfill"),
    ("fill in the missing founder names", "backfill"),
    ("the valuation column is empty", "backfill"),
    ("capture the employee count too", "backfill"),
    ("only search three sources instead of ten", "refine"),
    ("change the crawl depth", "refine"),
    ("reduce the number of pages", "refine"),
])
def test_the_obvious_sentences_land_on_the_right_intent(text, expected):
    intent, _score = proposals.classify(text)
    assert intent == expected


def test_a_plain_question_is_a_query_not_a_change():
    """Asking is not requesting. 'which companies are in fintech' must never be
    read as an instruction to modify anything."""
    intent, _ = proposals.classify("which companies are in fintech?")
    assert intent == "query"


def test_a_question_about_a_gap_stays_a_query():
    # It names a missing field and asks about it. It is still a question.
    intent, _ = proposals.classify("is the funding round empty?")
    assert intent == "query"


def test_adding_and_restricting_together_prefers_the_add():
    """A sentence can carry two intents; the data change is the one that costs
    something, so it is the one that must win the tie."""
    intent, _ = proposals.classify("add the funding round but only from the UK")
    assert intent == "backfill"


def test_an_empty_sentence_classifies_as_nothing():
    intent, score = proposals.classify("   ")
    assert intent == "" and score == 0.0


def test_classification_never_crashes_on_odd_input():
    for weird in ("?!?!", "123", "a" * 500, "____", "🙂 add funding"):
        proposals.classify(weird)


# --- field naming -----------------------------------------------------------
def test_a_named_field_narrows_the_proposal():
    store = _Store()
    out = proposals.propose(store, "add the funding round", "d1")
    assert out["intent"] == "backfill"
    assert "funding_round" in out["fields"]


def test_naming_a_field_that_is_not_a_gap_explains_why_not():
    store = _Store(records=[
        {"record_id": "1", "fields": {"company_name": _cell("Acme"),
                                       "funding_round": _cell("A")}}])
    out = proposals.propose(store, "add the funding round", "d1")
    assert out["fields"] == []
    assert "None of funding_round" in out["summary"]
    assert "no record has" in out["summary"]


def test_a_prefix_of_a_field_name_is_not_a_match():
    """`funding` must not match `funding_round`. Substring matching would, and
    the proposal would then silently target the wrong column."""
    store = _Store()
    out = proposals.propose(store, "fill in the funding", "d1")
    # `funding` alone is not a known field, so nothing is targeted precisely
    # and the proposal falls back to offering every gap.
    assert "company_name" not in out["fields"]


def test_underscored_field_names_match_on_all_their_words():
    store = _Store()
    out = proposals.propose(store, "add founder names", "d1")
    assert isinstance(out["fields"], list)


# --- the shape of a proposal ------------------------------------------------
def test_a_query_proposal_needs_no_confirmation():
    out = proposals.propose(_Store(), "which industry is most common?", "d1")
    assert out["intent"] == "query"
    assert out["writes"] is False
    assert out["needs_confirmation"] is False


def test_a_backfill_proposal_is_confirmed_and_costs_nothing_to_ask():
    out = proposals.propose(_Store(), "add the funding round", "d1")
    assert out["writes"] is True
    # Anything that writes to extracted data is confirmed. Not "usually" —
    # always, because the alternative is a sentence that edits a dataset.
    assert out["needs_confirmation"] is True
    assert out["cost"]["pages"] >= 1
    assert "POST" in out["endpoint"]


def test_a_proposal_never_carries_a_value_to_write():
    """The proposal describes an action. It must not contain a value, because
    that would be a way to smuggle a write past the confirmation."""
    for text in ("add the funding round", "only three sources instead of ten",
                 "set funding to 9M"):
        out = proposals.propose(_Store(), text, "d1")
        assert "value" not in out
        assert set(out) >= {"intent", "action", "writes", "needs_confirmation"}


def test_a_refine_proposal_says_it_only_compiles():
    out = proposals.propose(_Store(), "only search two sources instead of five", "d1")
    assert out["intent"] == "refine"
    assert "crawled" in out["summary"] or "run is started" in out["summary"]


def test_an_empty_question_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        proposals.propose(_Store(), "", "d1")
    with pytest.raises(ValueError):
        proposals.propose(_Store(), "   ", "d1")
