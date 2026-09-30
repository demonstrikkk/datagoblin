"""Phase-two gap filling: search the web for a field, without breaking the contract.

The tests concentrate on three things that can go wrong and would not be obvious
from the output:

* **A value attached to the wrong record.** Every search result that reaches a
  record has to arrive through the deduper's exact signature. A fuzzy match here
  would write a real value with a real quote to the wrong row, and nothing
  downstream would look wrong.
* **A gap closed on work that was never done.** A dry run, a failed search, and
  a search that returned nothing relevant are three different outcomes and only
  the last one is a finding.
* **A bill that is not bounded.** The pass-level cap is the only thing standing
  between one request and four thousand searches.
"""
from __future__ import annotations

import pytest

from app.services import gapfill as F
from app.services import gaps as G


# --- query construction ------------------------------------------------------

def test_a_depth_gap_query_names_the_entity():
    rec = {"record_id": "r1", "fields": {"company_name": "Acme Corp"}}
    qs = F.build_queries("funding_stage", rec, ["company_name"])
    assert qs == ['"Acme Corp" funding_stage']


def test_no_entity_means_no_query_rather_than_a_generic_one():
    """A query without an entity returns pages about other companies.

    Attaching their values to this record is the failure the identity match
    exists to prevent, so the correct output is no query at all.
    """
    rec = {"record_id": "r1", "fields": {"company_name": None, "city": "Berlin"}}
    assert F.build_queries("funding_stage", rec, ["company_name"]) == []


def test_a_very_short_identity_is_not_searched_for():
    """A one- or two-character identity matches everything and identifies nothing."""
    rec = {"record_id": "r1", "fields": {"ticker": "A"}}
    assert F.build_queries("pe_ratio", rec, ["ticker"]) == []


def test_a_sentence_shaped_identity_is_trimmed_to_something_searchable():
    """A 300-character query matches nothing and still costs the request."""
    rec = {"record_id": "r1", "fields": {
        "company_name": "Acme Corp " + ("and partners " * 60)}}
    q = F.build_queries("ceo", rec, ["company_name"])[0]
    assert len(q) < 140
    assert q.startswith('"Acme Corp and partners')


def test_entity_queries_respect_the_pass_level_budget():
    """The per-record limit alone would let 1,000 records queue 4,000 searches."""
    records = [{"record_id": f"r{i}", "fields": {"company_name": f"Company {i}"}}
               for i in range(50)]
    pairs = F.entity_queries("ceo", records, ["company_name"],
                             F.MAX_SEARCHES_PER_PASS)
    assert len(pairs) == F.MAX_SEARCHES_PER_PASS


def test_records_with_no_identity_consume_no_budget():
    records = [{"record_id": "r1", "fields": {}},
               {"record_id": "r2", "fields": {"company_name": "Acme Corp"}}]
    pairs = F.entity_queries("ceo", records, ["company_name"], 10)
    assert [r for r, _q in pairs] == ["r2"]


# --- result filtering --------------------------------------------------------

def test_an_entitys_own_site_is_worth_fetching():
    assert F.plausible("https://acmecorp.com/about", "Acme Corp")
    assert F.plausible("https://www.acmecorp.com/team", "acmecorp")


def test_a_roundup_about_other_companies_is_not():
    """The dominant result shape for a named company, and worthless as evidence."""
    assert not F.plausible(
        "https://techcrunch.com/best-series-a-climate-startups-2026", "Acme Corp")


def test_an_aggregator_page_that_mentions_the_entity_is_kept():
    assert F.plausible("https://crunchbase.com/organization/acme-corp", "Acme Corp")


def test_non_http_results_are_refused_before_they_are_fetched():
    assert not F.plausible("javascript:void(0)", "Acme Corp")
    assert not F.plausible("file:///etc/passwd", "Acme Corp")
    assert not F.plausible("", "Acme Corp")


# --- refusals ----------------------------------------------------------------

def test_an_evidence_gap_is_refused_without_searching():
    """Fetching more cannot add a verdict, so the spend would be pure waste."""
    with pytest.raises(F.GapFillRefused) as e:
        _run_with_gap({"state": "open", "category": "evidence_gap", "phase": 1,
                       "attempts": 1, "field": "ceo"})
    assert "judge or a person" in str(e.value)


def test_a_gap_with_no_recorded_history_is_refused():
    """A gap nobody derived is not a gap anybody may act on.

    Otherwise the endpoint becomes a way to run extraction for an arbitrary field
    with no coverage read in front of it.
    """

    class _Store:
        def get_gap(self, dataset_id, field):
            return None

    with pytest.raises(F.GapFillRefused) as e:
        import asyncio
        asyncio.run(F.run(_Store(), "d1", "ceo", phase=G.PHASE_SEARCH, llm=None,
                          search_fn=None, fetch_fn=None))
    assert "coverage view" in str(e.value)


def test_a_gap_already_searched_is_refused_a_second_time():
    """Idempotency. A double-submitted request must not run the same search twice."""
    with pytest.raises(F.GapFillRefused) as e:
        _run_with_gap({"state": "open", "category": "depth_gap",
                       "phase": G.PHASE_SEARCH, "attempts": 2, "field": "ceo"})
    assert "already been attempted" in str(e.value)


def test_an_exhausted_gap_is_refused():
    with pytest.raises(F.GapFillRefused) as e:
        _run_with_gap({"state": "exhausted", "category": "depth_gap",
                       "phase": G.PHASE_SEARCH, "attempts": 2, "field": "ceo",
                       "reason": "the sources available do not answer it"})
    assert "do not answer it" in str(e.value)


def test_an_unknown_phase_is_refused():
    with pytest.raises(F.GapFillRefused) as e:
        _run_with_gap({"state": "open", "category": "depth_gap", "phase": 0,
                       "attempts": 0, "field": "ceo"}, phase=7)
    assert "unknown phase" in str(e.value)


def test_a_dataset_without_dedupe_keys_searches_nothing():
    """A value found on the web could not be attached to any record, so searching
    would spend money to produce a page nobody can read."""
    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return []

    with pytest.raises(F.GapFillRefused) as e:
        _run_with_gap(
            {"state": "open", "category": "depth_gap", "phase": G.PHASE_STORED,
             "attempts": 1, "field": "ceo"},
            search_fn=_spy, dedupe_keys=[])
    assert "dedupe_keys" in str(e.value)
    assert calls == [], "nothing may be searched when the result cannot be attached"


# --- the attempt's reported outcome ------------------------------------------

def test_a_search_that_found_nothing_records_a_finding():
    """No results is a real answer: the sources do not carry this field."""
    after = G.record_attempt(
        {"state": "open", "phase": G.PHASE_STORED, "attempts": 1},
        phase=G.PHASE_SEARCH,
        stats={"searches_run": 3, "pages_fetched": 0, "cells_written": 0})
    assert after["state"] == "exhausted"
    assert after["phase"] == G.PHASE_SEARCH
    assert after["stats"]["searches_run"] == 3


def test_a_search_that_could_not_run_is_not_a_finding():
    """A provider failure must leave the gap open, not close it.

    The error raised is the typed one the search adapter actually raises. A
    genuinely unexpected exception is deliberately not swallowed: the request
    fails, the gap stays at phase 1, and nothing has been recorded as tried —
    which is the honest outcome, and better than a broad `except` that turns a bug
    into a false finding.
    """
    from app.core.errors import provider_transient

    async def _boom(*a, **k):
        raise provider_transient("search API 500")

    out = _run_with_gap(
        {"state": "open", "category": "depth_gap", "phase": G.PHASE_STORED,
         "attempts": 1, "field": "ceo"},
        search_fn=_boom, expect_ok=True)
    assert out["gap"]["state"] == "open", out["gap"]
    assert out["gap"]["phase"] == G.PHASE_SEARCH
    assert "failed" in out["gap"]["reason"]
    assert out["stats"]["searches_run"] >= 1, "the spend still has to be recorded"


def test_a_dry_run_that_found_values_leaves_the_gap_open():
    """Otherwise the next pass would refuse to search for a gap that is closable.

    A dry run that writes nothing has proved which pages answer, not that the
    answer was applied.
    """
    report = {"fillable": 3, "written": 0, "applied": False}
    stats = {"pages_read": 2, "cells_written": 0}
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    out = F._finish(gap, G.PHASE_SEARCH, report, stats, "")
    assert out["gap"]["state"] == "open"
    assert "dry run" in out["gap"]["reason"]


def test_an_applied_pass_that_wrote_cells_resolves_the_gap():
    report = {"fillable": 3, "written": 3, "applied": True}
    stats = {"pages_read": 2, "cells_written": 3}
    out = F._finish({"state": "open", "phase": G.PHASE_STORED, "attempts": 1},
                    G.PHASE_SEARCH, report, stats, "")
    assert out["gap"]["state"] == "resolved"


def test_the_attempt_stats_are_carried_onto_the_gap():
    """The cost has to survive, or "what did that cost" is unanswerable later."""
    out = F._finish({"state": "open", "phase": G.PHASE_STORED, "attempts": 1},
                    G.PHASE_SEARCH, {"fillable": 0, "written": 0},
                    {"searches_run": 4, "pages_fetched": 2, "cells_written": 0,
                     "pages_read": 2}, "")
    assert out["gap"]["stats"]["searches_run"] == 4
    assert out["gap"]["stats"]["pages_fetched"] == 2


# --- helpers -----------------------------------------------------------------

def _run_with_gap(gap, phase=G.PHASE_SEARCH, search_fn=None, dedupe_keys=None,
                 expect_ok=False):
    """Drive `gapfill.run` against a store complete enough for the real path.

    The plan is reached through the real `plan_for_dataset`, by giving the store
    the run and the plan it recovers from, rather than by patching the function
    under test — a monkeypatched plan would leave the code path that refuses an
    unattributable search untested.
    """
    import asyncio

    keys = ["company_name"] if dedupe_keys is None else dedupe_keys

    class _Store:
        def __init__(self):
            self.written = []

        def get_gap(self, dataset_id, field):
            return dict(gap)

        def get_dataset_row(self, dataset_id):
            return {"id": dataset_id, "run_id": "run1",
                    "schema": [{"name": "ceo"}, {"name": "company_name"}]}

        def get_run(self, run_id):
            return {"id": run_id, "workflow_id": "wf1"}

        def get_plan(self, workflow_id):
            return {"fields": [{"name": "ceo"}, {"name": "company_name"}],
                    "dedupe_keys": keys}

        def get_records(self, dataset_id, q="", limit=100, offset=0):
            return {"records": [
                {"record_id": "r1",
                 "fields": {
                     "company_name": {"value": "Acme Corp",
                                      "verification_status": "verified"}},
                 "_x": 1}]}

        def update_record_cell(self, dataset_id, record_id, field, cell):
            self.written.append((record_id, field, cell))
            return True

    async def _default_search(query, limit=4):
        return []

    return asyncio.run(F.run(
        _Store(), "d1", "ceo", phase=phase, llm=None,
        search_fn=search_fn or _default_search, fetch_fn=None))
