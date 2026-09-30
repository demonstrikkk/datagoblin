"""Gaps: what is missing, what has been tried, and what may be tried next.

The tests here are mostly about the *negative* cases, because that is where a
gap system lies. A gap tracker that overstates progress produces a UI that says
"we searched the web" for a field nobody searched, and "nothing found" for a
field nobody looked at. Both are worse than having no gap tracker at all, so
each invariant gets a test that would fail if it were quietly relaxed.
"""
from __future__ import annotations

import pytest

from app.services import gaps
from app.services import gaps as G


def cov(present=0, missing=0, unverified=0, conflicting=0, records=10):
    return {"records": records, "present": present, "missing": missing,
            "unverified": unverified, "conflicting": conflicting}


# --- classification ----------------------------------------------------------

def test_a_field_everywhere_empty_is_a_schema_gap():
    assert G.classify(cov(present=0, missing=10)) == "schema_gap"


def test_a_field_missing_from_some_records_is_a_depth_gap():
    assert G.classify(cov(present=7, missing=3)) == "depth_gap"


def test_a_field_with_values_but_no_verdicts_is_an_evidence_gap():
    assert G.classify(cov(present=10, unverified=10)) == "evidence_gap"


def test_conflicts_are_an_evidence_gap_even_when_nothing_is_missing():
    assert G.classify(cov(present=10, conflicting=2)) == "evidence_gap"


def test_a_fully_filled_verified_field_is_not_a_gap():
    """A gap list containing every field would be a gap list nobody can act on."""
    assert G.classify(cov(present=10)) is None


def test_evidence_outranks_depth_when_both_apply():
    """A field that is both partly empty and partly unproven needs judging first.

    Fetching more cannot give a value a verdict, so leading with the depth gap
    would send the reader after the wrong remedy.
    """
    assert G.classify(cov(present=5, missing=3, unverified=2)) == "evidence_gap"


# --- reasons -----------------------------------------------------------------

def test_the_schema_gap_reason_does_not_claim_the_sources_lack_the_field():
    c = G.category_reason("schema_gap", cov(present=0, missing=10, records=10))
    assert "do not answer it" in c
    # "the pages already fetched do not answer it" is scoped to the pages we have,
    # which is a claim we can make. A flat "this field does not exist" is not.
    assert "not exist" not in c


def test_the_depth_gap_reason_names_the_actual_counts():
    c = G.category_reason("depth_gap", cov(present=7, missing=3, records=10))
    assert "7 of 10" in c


def test_the_evidence_gap_reason_says_fetching_cannot_help():
    """The whole value of the category is that it redirects effort."""
    c = G.category_reason("evidence_gap", cov(present=10, unverified=4))
    assert "fetching more cannot help" in c


# --- the attempt ladder ------------------------------------------------------

def test_a_fresh_gap_starts_on_the_stored_pages_phase():
    gap = {"state": "open", "phase": G.PHASE_NONE, "attempts": 0}
    assert G.next_phase(gap) == G.PHASE_STORED


def test_a_gap_that_survived_stored_pages_escalates_to_search():
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    assert G.next_phase(gap) == G.PHASE_SEARCH


def test_the_ladder_never_skips_the_cheap_phase():
    """Phase 1 costs no external requests and can close a gap by itself.

    Going straight to a web search would spend money on a question the stored
    pages may already answer.
    """
    gap = {"state": "open", "phase": 0, "attempts": 0}
    assert G.next_phase(gap) != G.PHASE_SEARCH


def test_a_gap_that_has_been_through_search_has_no_next_phase():
    gap = {"state": "open", "phase": G.PHASE_SEARCH, "attempts": 2}
    assert G.next_phase(gap) is None


# --- the central invariant: phase records what ran ---------------------------

def test_a_phase_advances_even_when_the_attempt_found_nothing():
    """Otherwise a fruitless search is indistinguishable from an unmade one.

    This is the invariant the whole table exists for: a field we searched for and
    did not find must never be offered for searching again.
    """
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    out = G.record_attempt(gap, phase=G.PHASE_SEARCH,
                           stats={"searches_run": 3, "pages_fetched": 4, "cells_written": 0})
    assert out["phase"] == G.PHASE_SEARCH
    assert out["state"] == "exhausted"
    assert out["attempts"] == 2


def test_a_failed_attempt_advances_the_phase_but_leaves_the_gap_open():
    """A network failure is not evidence that the sources lack the field.

    Collapsing the two would let one timeout permanently close a gap, and the
    system would report a finding it never made.
    """
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    out = G.record_attempt(gap, phase=G.PHASE_SEARCH, error="search API timed out")
    assert out["state"] == "open", out
    assert out["phase"] == G.PHASE_SEARCH
    assert "timed out" in out["reason"]


def test_a_successful_attempt_that_wrote_cells_resolves_the_gap():
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    out = G.record_attempt(gap, phase=G.PHASE_SEARCH, stats={"cells_written": 4})
    assert out["state"] == "resolved"


def test_phase_never_moves_backwards():
    """A late phase-1 result must not undo a recorded phase-2 attempt."""
    gap = {"state": "open", "phase": G.PHASE_SEARCH, "attempts": 2}
    out = G.record_attempt(gap, phase=G.PHASE_STORED, stats={"cells_written": 1})
    assert out["phase"] == G.PHASE_SEARCH


# --- terminal states ---------------------------------------------------------

def test_an_exhausted_gap_refuses_both_phases_with_a_reason():
    gap = {"state": "exhausted", "phase": G.PHASE_SEARCH, "attempts": 2,
           "reason": "the sources available do not answer it"}
    for phase in (G.PHASE_STORED, G.PHASE_SEARCH):
        allowed, why = G.can_attempt(gap, phase=phase)
        assert not allowed
        assert why, "a refusal that explains nothing looks like a silent no-op"


def test_refusing_does_not_count_as_an_attempt():
    """Nothing was tried, so nothing may be recorded as tried."""
    gap = {"state": "open", "phase": G.PHASE_NONE, "attempts": 0}
    out = G.refusal(gap, "this plan declares no dedupe_keys")
    assert out["state"] == "refused"
    assert out["attempts"] == 0
    assert out["phase"] == G.PHASE_NONE
    assert G.next_phase(out) is None


def test_refused_is_distinct_from_exhausted():
    """Both are terminal, and merging them would report a conclusion from no work."""
    refused = G.refusal({"state": "open", "phase": 0, "attempts": 0}, "unsafe")
    exhausted = G.record_attempt({"state": "open", "phase": G.PHASE_STORED, "attempts": 1},
                                 phase=G.PHASE_SEARCH, stats={"cells_written": 0})
    assert refused["state"] != exhausted["state"]


def test_a_gap_at_the_attempt_cap_stops():
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": G.MAX_ATTEMPTS}
    assert G.next_phase(gap) is None
    allowed, why = G.can_attempt(gap, phase=G.PHASE_SEARCH)
    assert not allowed and str(G.MAX_ATTEMPTS) in why


def test_repeating_a_phase_is_refused():
    """Idempotency: a double-submitted request must not run the same search twice."""
    gap = {"state": "open", "phase": G.PHASE_STORED, "attempts": 1}
    allowed, why = G.can_attempt(gap, phase=G.PHASE_STORED)
    assert not allowed
    assert "already been attempted" in why


# --- ordering and totals -----------------------------------------------------

def test_derive_orders_by_outstanding_cells_not_by_field_count():
    coverage = {"fields": [
        {"field": "small", "records": 10, "present": 9, "missing": 1,
         "unverified": 0, "conflicting": 0},
        {"field": "big", "records": 10, "present": 0, "missing": 10,
         "unverified": 0, "conflicting": 0},
    ]}
    rows = G.derive(coverage)
    assert [r["field"] for r in rows] == ["big", "small"]
    assert rows[0]["outstanding"] == 10


def test_summary_separates_a_queue_from_a_set_of_findings():
    """`gaps: 40` on a dataset with forty exhausted gaps is the wrong headline."""
    rows = [{"category": "schema_gap", "state": "exhausted", "outstanding": 10}] * 40
    s = G.summary(rows)
    assert s["gaps"] == 40
    assert s["attemptable"] == 0
    assert s["outstanding_cells"] == 400


def test_summary_counts_every_category_key_even_at_zero():
    """A missing key would render as undefined rather than as zero."""
    s = G.summary([{"category": "depth_gap", "state": "open", "outstanding": 3}])
    assert set(s["by_category"]) == set(G.CATEGORIES)
    assert s["by_category"]["schema_gap"] == 0
    assert s["attemptable"] == 1


# --- reconciliation with what was tried --------------------------------------

class _Store:
    """Minimal stand-in for the gap surface of either adapter."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.writes = 0

    def list_gaps(self, dataset_id, *, state=None):
        return [r for (d, _f), r in self.rows.items() if d == dataset_id]

    def get_gap(self, dataset_id, field):
        return self.rows.get((dataset_id, field))

    def upsert_gap(self, dataset_id, field, gap):
        self.writes += 1
        row = {**gap, "dataset_id": dataset_id, "field": field}
        self.rows[(dataset_id, field)] = row
        return row


def test_sync_keeps_attempt_history_across_re_scans():
    """Re-deriving counts must not forget that a field was already searched.

    A dataset re-scanned on every page load would otherwise reset to phase 0 and
    run the same search forever, which is the exact failure the table prevents.
    """
    store = _Store()
    coverage = {"fields": [{"field": "funding", "records": 10, "present": 4,
                            "missing": 6, "unverified": 0, "conflicting": 0}]}
    gaps.sync(store, "d1", coverage)
    row = store.get_gap("d1", "funding")
    row = G.record_attempt(row, phase=G.PHASE_STORED, stats={"cells_written": 0})
    store.upsert_gap("d1", "funding", row)

    gaps.sync(store, "d1", coverage)
    again = store.get_gap("d1", "funding")
    assert again["phase"] == G.PHASE_STORED
    assert again["attempts"] == 1


def test_sync_opens_a_gap_again_if_a_filled_field_empties():
    """A field can lose its value when a record is replaced.

    Leaving it `resolved` would hide real missing data behind a stale success.
    """
    store = _Store()
    store.upsert_gap("d1", "funding", {
        "field": "funding", "category": "depth_gap", "state": "resolved",
        "phase": G.PHASE_SEARCH, "attempts": 2, "stats": {"cells_written": 6},
        "missing": 0, "unverified": 0, "conflicting": 0, "outstanding": 0,
    })
    coverage = {"fields": [{"field": "funding", "records": 10, "present": 0,
                            "missing": 10, "unverified": 0, "conflicting": 0}]}
    rows = gaps_by_field(gaps.sync(store, "d1", coverage), "funding")
    assert rows["state"] == "open"
    # The old attempt described a different state of the data.
    assert rows["phase"] == 0
    assert rows["attempts"] == 0


def test_sync_does_not_credit_a_fix_that_never_happened():
    """A field that was never a gap must not be recorded as resolved.

    Otherwise the first coverage scan would "close" every complete field by
    having attempted nothing for it.
    """
    store = _Store()
    coverage = {"fields": [{"field": "name", "records": 10, "present": 10,
                            "missing": 0, "unverified": 0, "conflicting": 0}]}
    out = gaps.sync(store, "d1", coverage)
    assert [r for r in out if r["field"] == "name"] == []
    assert store.get_gap("d1", "name") is None


def test_sync_degrades_when_the_adapter_cannot_track():
    """Derived coverage must still work if the gap table is unavailable.

    Losing the history is a real loss and `tracked` reports it; a 500 on a working
    coverage view would not be.
    """

    class _NoGaps:
        def list_gaps(self, dataset_id, *, state=None):
            raise AssertionError("must not be consulted")

    coverage = {"fields": [{"field": "funding", "records": 10, "present": 4,
                            "missing": 6, "unverified": 0, "conflicting": 0}]}
    assert G.tracked(_NoGaps()) is False
    assert G.derive(coverage)[0]["field"] == "funding"


def gaps_by_field(rows, name):
    return next(r for r in rows if r["field"] == name)


# --- the append-only adapter -------------------------------------------------
# The Postgres adapter gets convergence from a unique index. The local adapter is
# append-only JSONL and has to reproduce it by reading the last line per key, and
# the parity test only checks that both adapters *have* the methods. Without
# these, the local adapter could quietly return one row per write and every
# attempt count would be a lie while production stayed correct.

def test_the_local_adapter_converges_repeated_writes_to_one_row(tmp_path):
    from app.repositories.local_repo import LocalRepo

    store = LocalRepo(str(tmp_path))
    for _ in range(3):
        store.upsert_gap("d1", "funding", {
            "field": "funding", "category": "depth_gap", "state": "open",
            "phase": 0, "attempts": 0, "missing": 6, "outstanding": 6})

    assert len(store.list_gaps("d1")) == 1
    # Three lines were written, and the logical row is still one.
    disk = (tmp_path / "dataset_gaps.jsonl").read_text(encoding="utf-8").strip()
    assert len(disk.splitlines()) == 3


def test_the_local_adapter_reads_the_last_write_for_a_field(tmp_path):
    from app.repositories.local_repo import LocalRepo

    store = LocalRepo(str(tmp_path))
    store.upsert_gap("d1", "funding", {
        "field": "funding", "category": "depth_gap", "state": "open",
        "phase": 1, "attempts": 1, "missing": 6, "outstanding": 6})
    store.upsert_gap("d1", "funding", {
        "field": "funding", "category": "depth_gap", "state": "exhausted",
        "phase": 2, "attempts": 2, "missing": 6, "outstanding": 6})

    row = store.get_gap("d1", "funding")
    assert row["state"] == "exhausted"
    assert row["phase"] == 2
    assert row["attempts"] == 2


def test_the_local_adapter_orders_and_filters_gaps(tmp_path):
    from app.repositories.local_repo import LocalRepo

    store = LocalRepo(str(tmp_path))
    store.upsert_gap("d1", "small", {
        "field": "small", "category": "depth_gap", "state": "open",
        "phase": 0, "attempts": 0, "missing": 1, "outstanding": 1})
    store.upsert_gap("d1", "big", {
        "field": "big", "category": "schema_gap", "state": "open",
        "phase": 0, "attempts": 0, "missing": 9, "outstanding": 9})
    store.upsert_gap("d2", "elsewhere", {
        "field": "elsewhere", "category": "depth_gap", "state": "open",
        "phase": 0, "attempts": 0, "missing": 5, "outstanding": 5})

    assert [r["field"] for r in store.list_gaps("d1")] == ["big", "small"]
    assert [r["field"] for r in store.list_gaps("d2")] == ["elsewhere"]
    assert store.get_gap("d1", "nope") is None
