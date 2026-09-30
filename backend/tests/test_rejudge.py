"""Re-judging the cells that were extracted right and never checked.

The safety properties here are all one-directional claims, which is why they are
worth testing separately from the arithmetic:

* a value is never changed or removed by a judgement
* `judgment_unavailable` is never promoted to something that implies a check
  happened when one did not
* a cell that already carries a verdict is left alone entirely
* the free deterministic path runs before the budget is charged, so a cap bounds
  the expensive thing

The last one matters more than it looks: charging the budget first is how the
live 400-call cap ended up spent on arithmetic while real judgements were refused.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import gaps as G
from app.services import rejudge as RJ


#: A quote that is genuinely on the stored page but whose value is NOT a literal
#: substring of it. Without this the free deterministic check settles every case
#: and the judge is never called â€” which is the case the budget tests cover.
ONPAGE = "Acme Corp is a Berlin-based climate software firm"


def _cell(value, status="judgment_unavailable", quote=ONPAGE,
          page_id="p1"):
    return {"value": value, "verification_status": status,
            "source": {"url": "https://x.test/p", "title": "t",
                       "quote": quote, "page_id": page_id}}


class _Store:
    """Records what was written, so "never removes data" can be asserted."""

    def __init__(self, records, pages=None):
        self.records = records
        self.pages = pages if pages is not None else {
            "p1": "Acme Corp is a Berlin-based climate software firm. Acme Corp raised 40 million in Series B."}
        self.writes = []
        self.cell_reads = 0

    def get_dataset_row(self, dataset_id):
        return {"id": dataset_id, "run_id": "r1", "schema": []}

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        return {"records": self.records, "total": len(self.records)}

    def get_pages_by_ids(self, ids):
        self.cell_reads += 1
        return [{"id": i, "markdown": self.pages.get(i, "")} for i in ids]

    def update_record_cell(self, dataset_id, record_id, field, cell):
        self.writes.append((record_id, field, cell))
        return True


def _recs(*fields):
    return [{"record_id": "r1", "fields": dict(f)} for f in fields]


def run(store, **kw):
    return asyncio.run(RJ.run(store, "d1", **kw))


# --- selection ---------------------------------------------------------------

def test_only_unjudged_cells_are_selected():
    recs = _recs({"a": _cell("x"), "b": _cell("y", status="verified"),
                  "c": _cell("z", status="unverified"), "d": _cell("w", status="conflicting")})
    picked = RJ._cells_needing_a_verdict(recs)
    assert [c["field"] for c in picked] == ["a"], \
        "a cell that already carries a verdict must not be re-decided"


def test_a_cell_with_no_evidence_is_not_selected():
    """Nothing to judge against. Not a failure, an extraction gap."""
    recs = _recs({"a": _cell("x", quote=""), "b": _cell("y", page_id="")})
    assert RJ._cells_needing_a_verdict(recs) == []


def test_a_plain_scalar_is_not_selected():
    """Stored before provenance; it has no status to improve."""
    recs = [{"record_id": "r1", "fields": {"a": "plain"}}]
    assert RJ._cells_needing_a_verdict(recs) == []


def test_one_field_can_be_targeted():
    recs = _recs({"a": _cell("x"), "b": _cell("y")})
    assert [c["field"] for c in RJ._cells_needing_a_verdict(recs, "b")] == ["b"]


# --- the free path runs first ------------------------------------------------

def test_a_value_inside_its_own_quote_costs_no_judge_call():
    """The commonest case, and arithmetic. `company_name` alone is 198 cells."""
    store = _Store(_recs({"company_name": _cell("Acme Corp")}))
    out = run(store, apply=False)
    assert out["settled_free"] == 1
    assert out["judge_calls"] == 0
    assert out["verified_now"] == 1


def test_a_quote_absent_from_the_page_is_settled_without_a_call_too():
    store = _Store(_recs({"x": _cell("v", quote="text nowhere on the page")}),
                   pages={"p1": "a completely different page"})
    out = run(store, apply=False)
    assert out["judge_calls"] == 0
    assert out["verified_now"] == 0
    assert out["disagreements"] == 1


def test_the_budget_bounds_only_the_cells_that_need_a_model():
    """A cap is supposed to bound the expensive thing.

    Charged before the deterministic check, the 400-call cap was spent on
    arithmetic that costs nothing, and a real judgement could be refused as
    `judgment_unavailable` while the run reported its budget consumed. So a pass
    of ten free cells must settle all ten even at a budget of one, and only the
    genuinely undecidable cells may be refused.
    """
    calls = {"n": 0}

    async def _judge(value, quote, text):
        calls["n"] += 1
        return {"judgment": "SUPPORTED", "confidence": 0.9, "provider": "fake"}

    # Free: the value is literally inside its own quote, so no interpretation is
    # needed. Hard: the quote is on the page but the value is not in it.
    free_cells = {f"free{i}": _cell("Berlin-based climate") for i in range(10)}
    hard_cells = {f"hard{i}": _cell(f"zzz{i}", quote=ONPAGE) for i in range(10)}
    recs = _recs({**free_cells, **hard_cells})

    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(_Store(recs), apply=False, judge_budget=3)
    finally:
        jev_mod.evidence_verification = original

    assert calls["n"] == 3, "the cap did not bound the provider calls"
    assert out["settled_free"] == 10, "arithmetic was refused by the cap"
    assert out["verified_now"] == 10 + 3, "free cells plus the three that were judged"
    assert out["still_unjudged"] == 7


def test_a_zero_budget_still_settles_what_is_free():
    recs = _recs({"company_name": _cell("Acme Corp")})
    out = run(_Store(recs), apply=False, judge_budget=0)
    assert out["settled_free"] == 1
    assert out["verified_now"] == 1


def test_the_budget_can_leave_cells_unjudged_and_says_so():
    """A cap that reports success while doing nothing is the failure to avoid."""
    calls = {"n": 0}

    async def _judge(value, quote, text):
        calls["n"] += 1
        return {"judgment": "SUPPORTED", "confidence": 0.9, "provider": "fake"}

    recs = _recs({f"f{i}": _cell(f"v{i}", quote=ONPAGE) for i in range(5)})
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(_Store(recs), apply=False, judge_budget=2)
    finally:
        jev_mod.evidence_verification = original
    assert calls["n"] == 2, "the cap was not honoured"
    assert out["still_unjudged"] == 3
    assert out["verified_now"] == 2


# --- the non-destructive contract --------------------------------------------

def test_a_supported_judgement_upgrades_the_status():
    async def _judge(value, quote, text):
        return {"judgment": "SUPPORTED", "confidence": 0.91, "provider": "fake"}

    store = _Store(_recs({"x": _cell("keepme", quote=ONPAGE)}))
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(store, apply=True)
    finally:
        jev_mod.evidence_verification = original
    assert out["verified_now"] == 1
    _rid, _f, cell = store.writes[0]
    assert cell["verification_status"] == "verified"
    assert cell["value"] == "keepme"


def test_a_disagreement_keeps_the_value_and_never_deletes_it():
    """The most important assertion in this file.

    `verify_field` returns `None` for NOT_SUPPORTED, which is right at extraction
    time because the cell was never in the dataset. Here the value is already
    stored with a quote and a page behind it and was accepted once already.
    Deleting it on a second opinion would make this path more destructive than
    the gap it closes.
    """
    async def _judge(value, quote, text):
        return {"judgment": "NOT_SUPPORTED", "confidence": 0.8, "provider": "fake"}

    store = _Store(_recs({"x": _cell("keepme", quote=ONPAGE)}))
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(store, apply=True)
    finally:
        jev_mod.evidence_verification = original

    assert out["disagreements"] == 1
    assert out["verified_now"] == 0
    _rid, _f, cell = store.writes[0]
    assert cell["value"] == "keepme", "the value was removed by a judgement"
    assert cell["verification_status"] == "unverified"
    assert cell.get("rejudged") == "not_supported", \
        "the disagreement must be recorded, not silently demoted"
    assert out["disagreement_examples"][0]["value"] == "keepme"


def test_uncertain_leaves_the_cell_exactly_as_it_was():
    """"We could not tell" is not an improvement on "we could not tell"."""
    async def _judge(value, quote, text):
        return {"judgment": "UNCERTAIN", "confidence": 0.5, "provider": "fake"}

    store = _Store(_recs({"x": _cell("keepme", quote=ONPAGE)}))
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(store, apply=True)
    finally:
        jev_mod.evidence_verification = original

    assert out["still_unjudged"] == 1
    assert out["verified_now"] == 0 and out["disagreements"] == 0
    assert store.writes == [], "an unresolved cell must not be written at all"


def test_an_unreachable_judge_leaves_the_cell_untouched():
    async def _judge(value, quote, text):
        return {"judgment": "JUDGMENT_UNAVAILABLE", "confidence": 0.0, "provider": "none"}

    store = _Store(_recs({"x": _cell("keepme", quote=ONPAGE)}))
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(store, apply=True)
    finally:
        jev_mod.evidence_verification = original
    assert out["still_unjudged"] == 1
    assert store.writes == []


def test_a_thrown_provider_error_does_not_stop_the_pass():
    """One bad call must not cost the other cells their verdict."""
    state = {"n": 0}

    async def _judge(value, quote, text):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("provider exploded")
        return {"judgment": "SUPPORTED", "confidence": 0.9, "provider": "fake"}

    recs = _recs({"a": _cell("va", quote=ONPAGE), "b": _cell("vb", quote=ONPAGE),
                  "c": _cell("vc", quote=ONPAGE)})
    import app.providers.decision.jev as jev_mod
    original = jev_mod.evidence_verification
    jev_mod.evidence_verification = _judge
    try:
        out = run(_Store(recs), apply=False)
    finally:
        jev_mod.evidence_verification = original
    assert out["verified_now"] == 2
    assert out["still_unjudged"] == 1


# --- dry run -----------------------------------------------------------------

def test_a_dry_run_writes_nothing():
    store = _Store(_recs({"company_name": _cell("Acme Corp")}))
    out = run(store, apply=False)
    assert out["verified_now"] == 1
    assert out["written"] == 0
    assert store.writes == []


# --- evidence retrieval ------------------------------------------------------

def test_each_page_is_read_once_not_once_per_cell():
    """The 992 cells cite 38 pages; reading per cell would be 992 reads of which
    954 are redundant â€” the same shape as the documented re-fetch defect."""
    recs = _recs({f"f{i}": _cell("Acme Corp") for i in range(50)})
    store = _Store(recs)
    run(store, apply=False)
    assert store.cell_reads == 1, f"{store.cell_reads} page reads for 50 cells"


def test_cells_whose_page_is_gone_are_reported_not_guessed():
    store = _Store(_recs({"a": _cell("x")}), pages={})
    out = run(store, apply=False)
    assert out["evidence_available"] == 0
    assert out["evidence_missing"] == 1
    assert out["verified_now"] == 0
    assert "different problem" in out["note"]


def test_an_empty_dataset_is_refused_with_a_reason():
    with pytest.raises(RJ.RejudgeRefused) as e:
        run(_Store([]))
    assert "no records" in str(e.value)


def test_nothing_to_do_is_not_an_error():
    store = _Store(_recs({"a": _cell("x", status="verified")}))
    out = run(store, apply=False)
    assert out["unjudged_found"] == 0
    assert out["written"] == 0
    assert "nothing to do" in out["note"]


def test_a_negative_budget_is_refused_rather_than_treated_as_zero():
    with pytest.raises(RJ.RejudgeRefused):
        run(_Store(_recs({"a": _cell("x")})), judge_budget=-1)


# --- agreement with the gap vocabulary ---------------------------------------

def test_only_an_evidence_gap_is_a_rejudge_candidate():
    """So the two features cannot disagree about what an evidence_gap is."""
    ok, _ = RJ.is_rejudgeable({"category": "evidence_gap", "state": "open",
                               "phase": 0, "attempts": 0})
    assert ok
    for other in ("schema_gap", "depth_gap"):
        bad, why = RJ.is_rejudgeable({"category": other, "state": "open",
                                      "phase": 0, "attempts": 0})
        assert not bad and "fetching, not judging" in why


def test_a_resolved_gap_is_not_a_rejudge_candidate():
    ok, why = RJ.is_rejudgeable({"category": "evidence_gap", "state": "resolved",
                                 "phase": 0, "attempts": 0, "reason": "filled"})
    assert not ok and why


