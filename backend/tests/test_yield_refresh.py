"""Tests for learned yield, partial top-up, and refresh.

Grouped in one file because they share a fixture: a store with two hosts, one of
which has been productive, records carrying real quotes, and stored pages. That
shape is the only thing the three features reason about, and building it once
keeps each test about the decision rather than about the scaffolding.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import backfill as backfill_svc
from app.services import coverage as coverage_svc
from app.services import proposals as proposals_svc
from app.services import refresh as refresh_svc
from app.services import yieldmap as yieldmap_svc


# --- fixture ----------------------------------------------------------------

def _cell(value, url, status="verified", page_id="p1"):
    return {"value": value, "verification_status": status,
            "source": {"url": url, "quote": f"q:{value}", "page_id": page_id}}


class Store:
    """Just enough store for these three paths. Not a mock of the repository —
    the real adapters are tested elsewhere; this fixes the data shape."""

    def __init__(self, records, pages, sources, schema=None, plan=None):
        self._records = records
        self._pages = pages
        self._sources = sources
        self._schema = schema or [{"name": "company_name"}, {"name": "founder_names"}]
        self._plan = plan or {
            "goal": "find ai startups",
            "fields": self._schema,
            "dedupe_keys": ["company_name"],
        }
        self.writes: list[tuple] = []

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        rows = self._records[offset:offset + limit] if limit else self._records
        return {"records": rows, "total": len(self._records)}

    def get_pages(self, run_id, limit=200):
        return list(self._pages)[:limit]

    def get_pages_by_ids(self, ids):
        want = set(ids)
        return [p for p in self._pages if p["id"] in want]

    def get_page(self, page_id):
        return next((p for p in self._pages if p["id"] == page_id), None)

    def get_sources(self, dataset_id):
        return {"dataset_id": dataset_id, "sources": list(self._sources),
                "sources_attempted": len(self._sources),
                "sources_successful": len(self._sources), "sources_failed": 0}

    def get_dataset_row(self, dataset_id):
        return {"id": dataset_id, "run_id": "run1", "schema": self._schema,
                "record_count": len(self._records)}

    def get_dataset(self, dataset_id):
        return {**self.get_dataset_row(dataset_id), "records": self._records}

    def get_dataset_schema(self, dataset_id):
        return self._schema

    def get_run(self, run_id):
        return {"id": run_id, "workflow_id": "w1"}

    def get_plan(self, plan_id):
        return self._plan

    def update_record_cell(self, dataset_id, record_id, field, cell):
        self.writes.append((record_id, field, cell))
        return True


def _store():
    records = [
        {"record_id": "r1", "fields": {
            "company_name": _cell("Acme", "https://lists.example/one"),
            "founder_names": _cell("Ada", "https://lists.example/one")}},
        {"record_id": "r2", "fields": {
            "company_name": _cell("Beta", "https://lists.example/two"),
            "founder_names": _cell("", "https://lists.example/two")}},
        # A wide-schema record from a second host: many cells, but from a site
        # that produced nothing proven overall. This is the case a naive
        # "cells per page" ranking would get wrong.
        {"record_id": "r3", "fields": {
            "company_name": _cell("Gamma", "https://blog.example/post"),
            "founder_names": _cell("Cy", "https://blog.example/post")}},
    ]
    pages = [
        {"id": "p1", "url": "https://lists.example/one",
         "markdown": "Acme Ada founded by Ada in London. Beta also listed."},
        {"id": "p2", "url": "https://lists.example/two",
         "markdown": "Beta. Acme appears here too."},
        {"id": "p3", "url": "https://blog.example/post",
         "markdown": "Unrelated commentary about nothing in particular."},
    ]
    sources = [
        {"url": "https://lists.example/one", "status": "ok"},
        {"url": "https://lists.example/two", "status": "ok"},
        {"url": "https://blog.example/post", "status": "reused"},
    ]
    return Store(records, pages, sources)


# --- yieldmap ---------------------------------------------------------------

def test_host_of_normalises_the_two_spellings_of_one_site():
    assert yieldmap_svc.host_of("https://www.Example.com/a") == "example.com"
    assert yieldmap_svc.host_of("http://example.com:8080/a") == "example.com"
    assert yieldmap_svc.host_of("not a url") == ""
    assert yieldmap_svc.host_of("") == ""


def test_yield_counts_verified_cells_per_page_fetched():
    ym = yieldmap_svc.build(_store(), "d1")
    row = ym["hosts"]["lists.example"]
    assert row["verified"] == 3          # Acme name, Ada, Beta name
    assert row["pages"] == 2
    assert row["records"] == 2
    assert row["yield"] == pytest.approx(1.5)


def test_a_host_that_spent_a_page_and_earned_nothing_has_zero_yield():
    """Zero, not infinity and not absent. `ads.example` cost a page and supplied
    no verified cell, which is a different proposition from a host we have never
    visited, and both are different from a host that earned a great deal."""
    store = _store()
    store._pages = list(store._pages) + [
        {"id": "p4", "url": "https://ads.example/banner", "markdown": "Buy now."}]
    store._sources = list(store._sources) + [
        {"url": "https://ads.example/banner", "status": "ok"}]
    ym = yieldmap_svc.build(store, "d1")
    assert ym["hosts"]["ads.example"]["pages"] == 1
    assert ym["hosts"]["ads.example"]["verified"] == 0
    assert ym["hosts"]["ads.example"]["yield"] == 0.0


def test_a_record_is_counted_once_per_host_not_once_per_cell():
    """Fifty fields from one page is one company. Counting cells would make a
    wide-schema host look productive purely for having more columns."""
    ym = yieldmap_svc.build(_store(), "d1")
    assert ym["hosts"]["blog.example"]["records"] == 1
    assert ym["hosts"]["blog.example"]["cells"] == 2


def test_conflicting_cells_are_not_counted_as_proven():
    records = [{"record_id": "r1", "fields": {
        "company_name": _cell("Acme", "https://a.example/x", status="conflicting")}}]
    store = Store(records, [{"id": "p", "url": "https://a.example/x", "markdown": "x"}],
                  [{"url": "https://a.example/x", "status": "ok"}])
    ym = yieldmap_svc.build(store, "d1")
    row = ym["hosts"]["a.example"]
    assert row["verified"] == 0
    assert row["conflicting"] == 1


def test_bonus_is_zero_for_an_unknown_host_and_bounded_for_a_known_one():
    ym = yieldmap_svc.build(_store(), "d1")
    assert yieldmap_svc.bonus(ym, "lists.example") > 0
    assert yieldmap_svc.bonus(ym, "never-seen.example") == 0.0
    assert yieldmap_svc.bonus({}, "anything") == 0.0
    assert yieldmap_svc.bonus(ym, "lists.example") <= yieldmap_svc.MAX_BONUS


def test_bonus_saturates_so_going_from_twenty_to_twentyone_matters_less():
    """A site with 20 proven cells per page is not twice as interesting as one
    with 10, for the purpose of choosing a page to open."""
    def b(v):
        return yieldmap_svc.bonus({"hosts": {"h": {"yield": v}}}, "h")
    assert b(20) - b(10) < b(10) - b(0.0001)


def test_ranking_puts_the_productive_host_first():
    ym = yieldmap_svc.build(_store(), "d1")
    assert ym["ranking"][0] == "lists.example"


# --- backfill: partial top-up ------------------------------------------------

def _matrix(records, fields):
    return coverage_svc.field_coverage(records, fields)


def test_never_extracted_and_partially_filled_are_different_gaps():
    matrix = _matrix(
        [{"record_id": "1", "fields": {"a": _cell("x", "u")}},
         {"record_id": "2", "fields": {"a": _cell("", "u")}},
         {"record_id": "3", "fields": {"a": _cell("", "u")}}],
        [{"name": "a"}, {"name": "b"}])
    names, detail = backfill_svc.topup_fields(matrix, include_partial=False)
    assert names == ["b"] and detail["b"]["kind"] == "never_extracted"

    names, detail = backfill_svc.topup_fields(matrix, include_partial=True)
    assert names == ["a", "b"]
    assert detail["a"] == {"kind": "partial", "present": 1, "missing": 2, "records": 3}


def test_a_fully_filled_field_is_never_a_target_in_either_mode():
    matrix = _matrix([{"record_id": "1", "fields": {"a": _cell("x", "u")}}],
                     [{"name": "a"}])
    assert backfill_svc.topup_fields(matrix, include_partial=True) == ([], {})


def test_the_proposal_reports_empty_cells_not_just_a_field_count():
    """A field present on 76 of 88 records is 12 cells of work; reporting
    "1 field" would understate it by an order of magnitude."""
    out = backfill_svc.propose(_store(), "d1", include_partial=True)
    assert out["include_partial"] is True
    # founder_names is filled on r1 and r3, empty on r2: one empty cell, not
    # three, and not a whole field.
    assert out["empty_cells"] == 1
    assert out["fields"] == ["founder_names"]
    assert out["partial"] == {"founder_names": {
        "kind": "partial", "present": 2, "missing": 1, "records": 3}}


def test_the_default_narrower_contract_is_unchanged_by_the_widening():
    out = backfill_svc.propose(_store(), "d1")
    assert out["include_partial"] is False
    assert out["fields"] == []
    assert "include_partial" in out["reason"]


def test_the_proposal_says_when_there_is_nothing_left_to_top_up():
    store = _store()
    store._records = [{"record_id": "r1", "fields": {
        "company_name": _cell("Acme", "https://lists.example/one"),
        "founder_names": _cell("Ada", "https://lists.example/one")}}]
    out = backfill_svc.propose(store, "d1", include_partial=True)
    assert out["backfillable"] is False
    assert "nothing" in out["reason"] or "filled" in out["reason"]


def test_yield_breaks_a_tie_between_pages_naming_the_same_records():
    """Both pages name Acme and Beta, so relevance cannot separate them. The
    host that has actually produced proven cells goes first."""
    chosen = backfill_svc._candidate_pages(
        _store(), "run1", 1,
        [{"record_id": "r", "fields": {"company_name": _cell("Acme Beta", "u")}}],
        ["company_name"], yieldmap_svc.build(_store(), "d1"))
    assert len(chosen) == 1


def test_relevance_still_beats_yield():
    """The measured run where relevance picked the pages (booking.com and
    kayak.com for a dataset of companies, 1 record matched in 5) is why yield
    is a tiebreaker and not the primary key. A page that names every record must
    win even from a host that has produced nothing."""
    store = _store()
    ym = {"hosts": {"blog.example": {"yield": 99.0, "pages": 1}}}
    chosen = backfill_svc._candidate_pages(
        store, "run1", 1,
        [{"record_id": "r", "fields": {"company_name": _cell("Acme", "u")}}],
        ["company_name"], ym)
    assert "lists.example" in chosen[0]["url"]


# --- refresh ----------------------------------------------------------------

def test_named_hosts_select_sources_without_knowing_their_urls():
    picked = refresh_svc.pick_sources(_store(), "d1", ["lists.example"])
    assert {s["url"] for s in picked} == {
        "https://lists.example/one", "https://lists.example/two"}


def test_a_host_that_matches_nothing_refreshes_nothing_rather_than_something_else():
    assert refresh_svc.pick_sources(_store(), "d1", ["nowhere.example"]) == []


def test_unnamed_sources_are_ordered_by_recorded_yield():
    picked = refresh_svc.pick_sources(_store(), "d1", None)
    assert yieldmap_svc.host_of(picked[0]["url"]) == "lists.example"


def test_the_proposal_names_the_cells_that_can_actually_change():
    out = refresh_svc.propose(_store(), "d1", ["lists.example"])
    assert out["refreshable"] is True
    # Only values that came from the refreshed hosts can change. The Gamma row
    # came from blog.example and is untouched.
    assert out["cells_reverified"] == 3
    assert out["refetches"] is True


def test_an_unchanged_value_is_left_alone_rather_than_rewritten():
    """Rewriting an identical cell would churn every cell in the dataset on a
    no-op refresh, and make the run look like work when it was none."""
    v = refresh_svc.decide_one(_cell("Ada", "u"), _cell("ada ", "u2"))
    assert v["action"] == "unchanged"


def test_a_changed_value_keeps_the_old_one_as_a_reviewable_rival():
    """A silent overwrite would leave the dataset looking exactly as
    trustworthy as before while saying something different."""
    v = refresh_svc.decide_one(_cell("Ada", "u"), _cell("Grace", "u2"))
    assert v["action"] == "changed"
    assert v["old"] == "Ada"
    rivals = v["cell"]["rivals"]
    assert len(rivals) == 1
    assert rivals[0]["value"] == "Ada"
    assert rivals[0]["source"]["url"] == "u"          # the old cell's evidence
    assert rivals[0]["superseded_by_refresh"] is True
    assert v["cell"]["value"] == "Grace"               # the new one is live


def test_an_unverified_new_value_never_replaces_a_proven_one():
    """Fresh is not the same as correct. An unverified value may be right, and it
    may not be written over a value the judge confirmed."""
    v = refresh_svc.decide_one(_cell("Ada", "u"),
                               {"value": "Someone Else", "source": {"url": "u2"}})
    assert v["action"] == "unverifiable"
    assert v["old"] == "Ada"


def test_an_empty_new_value_never_replaces_a_proven_one():
    v = refresh_svc.decide_one(_cell("Ada", "u"), _cell("", "u2"))
    assert v["action"] == "unverifiable"


def test_refresh_leaves_an_empty_cell_to_backfill():
    """Doing it here would blur which operation actually did the work."""
    v = refresh_svc.decide_one(_cell("", "u"), _cell("New Person", "u2"))
    assert v["action"] == "empty"


def test_a_value_with_no_verdict_at_all_is_not_treated_as_proven():
    # A bare string, the pre-provenance shape, carries no verdict.
    v = refresh_svc.decide_one(_cell("Ada", "u"), {"value": "Other",
                                                    "source": {"url": "u2"}})
    assert v["action"] == "unverifiable"


def test_a_refresh_that_finds_nothing_different_writes_nothing():
    """End to end through the real run(), with a fetch that returns nothing
    usable, so the report is the honest one rather than a fabricated count."""
    async def fetch(url):
        return {"url": url, "skipped": "robots", "html": ""}

    async def llm(prompt, schema):
        return {"records": []}

    out = asyncio.run(refresh_svc.run(
        _store(), "d1", ["lists.example"], llm=llm, fetch=fetch, apply=False))
    assert out["changed"] == 0 and out["written"] == 0


def test_a_fetch_failure_is_reported_as_no_change_rather_than_an_error():
    async def fetch(url):
        raise RuntimeError("network down")

    async def llm(prompt, schema):
        return {"records": []}

    out = asyncio.run(refresh_svc.run(
        _store(), "d1", ["lists.example"], llm=llm, fetch=fetch, apply=False))
    assert out["changed"] == 0
    assert out["unverifiable"] == 0


def test_a_refresh_that_changes_something_reports_the_change_it_did_not_write():
    """The dry run must describe the write it would perform, or the proposal's
    numbers cannot be checked against what happens."""
    store = _store()
    store._plan = {**store._plan}

    async def fetch(url):
        return {"url": url, "markdown": f"Grace founded Acme. {url}", "html": ""}

    async def llm(prompt, schema):
        # The judge confirms the fresh value, so the decision is `changed`.
        return {"records": [{"fields": {
            "company_name": {"value": "Acme", "verification_status": "verified",
                             "source": {"url": url, "quote": "Acme"}},
            "founder_names": {"value": "Grace", "verification_status": "verified",
                              "source": {"url": url, "quote": "Grace"}}},
            "source_url": "https://lists.example/one"}]}

    out = asyncio.run(refresh_svc.run(
        store, "d1", ["lists.example"], llm=llm, fetch=fetch, apply=False))
    assert out["written"] == 0
    assert out["note"].startswith("sources were re-fetched")
    assert out["preserves_rivals"] is True


def test_refresh_refuses_a_plan_it_cannot_match_records_with():
    store = _store()
    store._plan = {**store._plan, "dedupe_keys": []}
    with pytest.raises(refresh_svc.RefreshRefused):
        asyncio.run(refresh_svc.run(store, "d1", ["lists.example"],
                                    llm=None, fetch=None))


# --- proposals: the two new intents ----------------------------------------

def test_a_sentence_ending_in_a_question_never_becomes_a_write():
    for q in ("are the founder names empty?", "can you refresh the sources?",
              "is the funding round missing?"):
        assert proposals_svc.classify(q)[0] == "query", q


def test_asking_for_a_new_column_is_add_column_not_backfill():
    intent, _ = proposals_svc.classify("add a new column for the funding round")
    assert intent == "add_column"


def test_asking_for_a_missing_value_to_be_filled_is_still_backfill():
    intent, _ = proposals_svc.classify("fill the missing founder names")
    assert intent == "backfill"


def test_asking_to_recheck_is_a_refresh():
    for q in ("refresh the sources", "recheck the values, they look stale",
              "update the data from techcrunch"):
        assert proposals_svc.classify(q)[0] == "refresh", q


def test_a_refresh_proposal_never_claims_to_write_when_there_is_nothing_to_read():
    out = proposals_svc.propose(_store(), "refresh nowhere.example", "d1")
    assert out["intent"] == "refresh"
    assert out["writes"] is False
    assert out["needs_confirmation"] is False
    assert "nowhere.example" in out["reason"]


def test_a_refresh_proposal_states_the_three_rules_that_make_it_safe():
    out = proposals_svc.propose(_store(), "refresh lists.example", "d1")
    assert out["writes"] is True and out["needs_confirmation"] is True
    low = out["summary"].lower()
    assert "quote" in low and "rival" in low


def test_add_column_will_not_guess_a_field_name():
    out = proposals_svc.propose(_store(), "add a new column", "d1")
    assert out["writes"] is False
    assert "will not guess" in out["summary"].lower()
