"""The automatic gap-fill pass a run makes over the dataset it just saved.

The pass reuses `gapfill.run` — the same code the on-demand route drives — so
these tests patch that one function and assert on how it is driven: which
fields, which phases, how much search budget, and never with `apply=False`.
Everything else (coverage, gap derivation, budget accounting, the ladder) is the
real code.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import autofill as autofill_svc
from app.services import gapfill as gapfill_svc
from app.services import gaps as gaps_svc


# --- fixtures ----------------------------------------------------------------

def _records(*, hq_present=5, hq_missing=1, extra_field=None):
    recs = []
    for i in range(hq_present + hq_missing):
        fields = {"company_name": {"value": f"Co {i}",
                                   "verification_status": "verified"}}
        if i < hq_missing:
            fields["headquarters_city"] = {"value": ""}
        else:
            fields["headquarters_city"] = {"value": "Berlin",
                                           "verification_status": "verified"}
        if extra_field:
            for name, cell in extra_field.items():
                fields.setdefault(name, dict(cell))
        recs.append({"record_id": f"r{i}", "fields": fields})
    return recs


_PLAN = {"fields": [{"name": "company_name"}, {"name": "headquarters_city"},
                    {"name": "founded_year"}]}


class _Store:
    """The slice of the repository surface the pass reads and writes."""

    def __init__(self, records, *, gaps_history: bool = True):
        self._records = records
        self._gaps: dict[str, dict] = {}
        if not gaps_history:
            self.upsert_gap = None

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        return {"records": list(self._records), "total": len(self._records)}

    def list_gaps(self, dataset_id, *, state=None):
        return list(self._gaps.values())

    def get_gap(self, dataset_id, field):
        g = self._gaps.get(field)
        return dict(g) if g else None

    def upsert_gap(self, dataset_id, field, gap):
        self._gaps[field] = {**gap, "field": field}
        return self._gaps[field]

    def update_record_cell(self, dataset_id, record_id, field, cell):
        return True


def _patch_run(monkeypatch, calls, *, fill_per_phase=None, searches_per_phase=None,
               refuse=False):
    """Replace `gapfill.run` with a double that keeps the real gap semantics.

    The state it returns is produced by the real `gaps.record_attempt`, so the
    ladder the pass walks (open → resolved, or "miss stays open so phase 2 can
    run") is the production one, not a re-statement of it.
    """
    fill_per_phase = fill_per_phase or {}
    searches_per_phase = searches_per_phase or {}

    async def run(store, dataset_id, field, *, phase, llm, search_fn, fetch_fn,
                  persist_page=None, search_budget=12, apply=False,
                  phrases=None):
        calls.append({"field": field, "phase": phase, "apply": apply,
                      "search_budget": search_budget, "phrases": phrases})
        if refuse:
            raise gapfill_svc.GapFillRefused("no identity to match against")
        gap = store.get_gap(dataset_id, field)
        written = int(fill_per_phase.get(phase, 0))
        searches = int(searches_per_phase.get(phase, 0))
        stats = {"cells_written": written, "searches_run": searches}
        report = {"written": written, "searches_run": searches,
                  "fillable": written, "applied": apply}
        after = gaps_svc.record_attempt(
            gap, phase=phase, stats=stats,
            error="" if written else "no value found")
        return {"report": report, "gap": after}

    monkeypatch.setattr(gapfill_svc, "run", run)


def _fill(store, plan, **kw):
    kw.setdefault("search_budget", 3)
    kw.setdefault("max_fields", 2)
    return asyncio.run(autofill_svc.fill_open_gaps(
        store=store, dataset_id="d1", plan=plan, llm=None, search_fn=None,
        fetch_fn=None, **kw))


# --- what it refuses to do ---------------------------------------------------

def test_it_is_disabled_without_spending_a_search(monkeypatch):
    monkeypatch.setattr(settings, "RUN_AUTOFILL_SEARCHES", 0)
    calls = []
    _patch_run(monkeypatch, calls)
    out = _fill(_Store(_records()), _PLAN, search_budget=None, max_fields=None)
    assert out["skipped"] == "automatic gap-fill is disabled"
    assert calls == []
    assert out["cells_written"] == 0


def test_a_store_without_gap_history_is_skipped(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls)
    out = _fill(_Store(_records(), gaps_history=False), _PLAN)
    assert "does not track gap history" in out["skipped"]
    assert calls == []


def test_an_evidence_gap_is_never_attempted(monkeypatch):
    # A value with no verdict is an evidence gap; fetching cannot add a verdict,
    # so it is not a candidate and no search is spent on it.
    recs = _records(extra_field={"website": {"value": "https://example.com"}})
    calls = []
    _patch_run(monkeypatch, calls)
    out = _fill(_Store(recs), _PLAN)
    fields = {e["field"] for e in out["fields"]}
    assert "website" not in fields
    assert all(c["field"] != "website" for c in calls)


def test_a_dominant_absence_is_filled_even_with_one_conflict(monkeypatch):
    """One disputed cell must not hide 122 empty ones.

    Run `890f3509` ended with `chief_executive` present on 6 of 128 records, one
    of them conflicting. Every field was therefore `evidence_gap`, the pass
    considered nothing, and 122 cells stayed empty without a search attempt.
    """
    recs = []
    for i in range(6):
        cell = ({"value": "", "verification_status": "unverified"} if i < 5
                else {"value": "Jane Doe", "verification_status": "conflicting"})
        recs.append({"record_id": f"r{i}",
                     "fields": {"company_name": {"value": f"Co {i}",
                                                 "verification_status": "verified"},
                                "chief_executive": cell}})
    plan = {"fields": [{"name": "company_name"}, {"name": "chief_executive"}]}
    calls = []
    _patch_run(monkeypatch, calls, fill_per_phase={1: 0, 2: 4},
               searches_per_phase={2: 4})

    out = _fill(_Store(recs), plan)

    entry = next(e for e in out["fields"] if e["field"] == "chief_executive")
    assert entry["searches_run"] == 4
    assert entry["cells_written"] == 4
    # Search first: the stored pages are where the absence came from, and phase
    # 1 resolves the gap on its first write, which ends the pass early.
    assert entry["phases"] == [2]
    assert all(c["phase"] == 2 for c in calls)


def test_a_dominant_verdict_gap_is_still_not_searched(monkeypatch):
    """The opposite case: mostly unjudged values, one empty cell."""
    recs = []
    for i in range(6):
        cell = ({"value": "", "verification_status": "unverified"} if i == 0
                else {"value": f"person {i}",
                      "verification_status": "unverified"})
        recs.append({"record_id": f"r{i}",
                     "fields": {"company_name": {"value": f"Co {i}",
                                                 "verification_status": "verified"},
                                "chief_executive": cell}})
    plan = {"fields": [{"name": "company_name"}, {"name": "chief_executive"}]}
    calls = []
    _patch_run(monkeypatch, calls)

    out = _fill(_Store(recs), plan)

    assert out["fields"] == []
    assert calls == []


# --- the ladder, and what it costs ------------------------------------------

def test_a_depth_gap_is_searched_and_the_fill_is_counted(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls, fill_per_phase={2: 3},
               searches_per_phase={2: 2})
    out = _fill(_Store(_records()), _PLAN)
    entry = next(e for e in out["fields"] if e["field"] == "headquarters_city")
    assert entry["phases"] == [2] or entry["phases"] == [1, 2]
    assert entry["cells_written"] == 3
    assert out["searches_run"] == 2
    # Every attempt was applied; a dry run would be meaningless here.
    assert all(c["apply"] is True for c in calls)


def test_an_edition_phrase_reaches_the_search(monkeypatch):
    # The pass has the plan in hand; an edition's vocabulary has to reach the
    # query builder through it, or the edition would only affect the first crawl.
    calls = []
    _patch_run(monkeypatch, calls, fill_per_phase={2: 1}, searches_per_phase={2: 1})
    plan = {**_PLAN, "field_phrases": {"headquarters_city": "head office"}}
    _fill(_Store(_records()), plan)
    assert any(c["phrases"] == {"headquarters_city": "head office"}
               for c in calls)


def test_a_schema_gap_is_never_searched(monkeypatch):
    # No source has ever carried `founded_year`, so searching is a proposal, not
    # a default: phase 1 only.
    calls = []
    _patch_run(monkeypatch, calls, fill_per_phase={1: 0}, searches_per_phase={2: 5})
    out = _fill(_Store(_records()), _PLAN, max_fields=1)
    entry = next(e for e in out["fields"] if e["field"] == "founded_year")
    assert entry["phases"] == [1]
    assert entry["searches_run"] == 0
    assert "proposal" in entry.get("note", "")
    assert all(c["phase"] == 1 for c in calls)


def test_the_search_budget_bounds_the_whole_pass(monkeypatch):
    # Two depth gaps, one search of budget. The first spends it; the second must
    # not search at all.
    recs = _records()
    for r in recs:
        if r["fields"]["headquarters_city"]["value"] == "":
            r["fields"]["hq_region"] = {"value": ""}
        else:
            r["fields"]["hq_region"] = {"value": "Europe",
                                        "verification_status": "verified"}
    plan = {"fields": [{"name": "company_name"}, {"name": "headquarters_city"},
                       {"name": "hq_region"}]}
    calls = []
    _patch_run(monkeypatch, calls, fill_per_phase={1: 0}, searches_per_phase={2: 1})
    out = _fill(_Store(recs), plan, max_fields=2, search_budget=1)
    assert out["searches_run"] == 1
    searched = [c for c in calls if c["phase"] == 2]
    assert len(searched) == 1, "the second gap must not get a phase-2 search"
    spent = next(e for e in out["fields"] if e["searches_run"] == 1)
    unspent = next(e for e in out["fields"] if e["searches_run"] == 0)
    assert "budget spent" in unspent.get("note", "")
    assert spent["field"] != unspent["field"]


def test_a_refusal_is_recorded_and_does_not_abort_the_pass(monkeypatch):
    calls = []
    _patch_run(monkeypatch, calls, refuse=True)
    out = _fill(_Store(_records()), _PLAN)
    assert out["fields"], "a refusal must still be reported, not silently dropped"
    assert any("refused" in e for e in out["fields"])
    assert out["cells_written"] == 0


# --- the runner's wiring -----------------------------------------------------

def test_the_runner_helper_is_a_no_op_without_a_repo(monkeypatch):
    from app.services import runner
    calls = []

    async def _spy(**k):
        calls.append(k)
        return {}

    monkeypatch.setattr(runner.autofill_svc, "fill_open_gaps", _spy)
    out = asyncio.run(runner._autofill_pass(
        None, "r1", _PLAN, None, "d1", [{"record_id": "r0"}],
        None, None, None, None))
    assert out == {}
    assert calls == []


def test_the_runner_helper_respects_the_off_switch(monkeypatch):
    from app.services import runner
    monkeypatch.setattr(settings, "RUN_AUTOFILL", False)
    calls = []

    async def _spy(**k):
        calls.append(k)
        return {}

    monkeypatch.setattr(runner.autofill_svc, "fill_open_gaps", _spy)
    out = asyncio.run(runner._autofill_pass(
        None, "r1", _PLAN, object(), "d1", [{"record_id": "r0"}],
        None, None, None, None))
    assert out == {} and calls == []


def test_the_runner_helper_hands_the_pass_the_repo(monkeypatch):
    from app.services import runner
    seen = {}

    async def _spy(**k):
        seen.update(k)
        return {"cells_written": 1}

    monkeypatch.setattr(runner.autofill_svc, "fill_open_gaps", _spy)
    repo = object()
    out = asyncio.run(runner._autofill_pass(
        None, "r1", _PLAN, repo, "d1", [{"record_id": "r0"}],
        None, None, None, None))
    assert seen["store"] is repo
    assert seen["dataset_id"] == "d1"
    assert out["cells_written"] == 1
