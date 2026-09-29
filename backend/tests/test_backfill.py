"""Backfill must fill gaps without ever corrupting a row.

Most of what matters here is what it must *refuse to do*. A backfill that
attaches a value to the wrong record produces a dataset that looks correct:
the value has a real quote, a real page, and a real verdict, because it went
through the same validator a run uses. Nothing downstream can tell. So the
negative cases get as much attention as the positive ones.
"""
import asyncio

import pytest

from app.services import backfill as bf


# --- fixtures ---------------------------------------------------------------
def _cell(value, status="unverified", quote="q", url="https://x.example/",
          page_id="p1"):
    return {"value": value, "verification_status": status,
            "source": {"quote": quote, "url": url, "page_id": page_id}}


def _rec(rid, **fields):
    return {"record_id": rid, "fields": fields}


class _Store:
    """Just the repository surface backfill uses."""

    def __init__(self, dataset=None, records=None, pages=None, plan=None):
        self.dataset = dataset if dataset is not None else {
            "id": "d1", "run_id": "r1", "schema": [{"name": "name"}, {"name": "funding"}]}
        self.records = records or []
        # A stored page always carries evidence text; a page with neither
        # markdown nor raw_html is unreadable and the service correctly
        # refuses to work from it.
        self.pages = pages if pages is not None else [
            {"id": "p1", "url": "https://x.example/", "markdown": "Acme raised $9M",
             "raw_html": "<html><body>Acme raised $9M</body></html>",
             "title": "t", "content_hash": "h", "method": "http",
             "run_id": "r1", "retrieved_at": "2026-09-01T00:00:00Z"}]
        self.plan = plan if plan is not None else {
            "fields": [{"name": "name"}, {"name": "funding"}],
            "dedupe_keys": ["name"]}
        self.written: list[tuple] = []

    # -- repository
    def get_dataset(self, did):
        return self.dataset if did == "d1" else None

    def get_run(self, rid):
        return {"id": rid, "workflow_id": "w1"} if rid == "r1" else None

    def get_plan(self, pid):
        return self.plan if pid == "w1" else None

    def get_records(self, did, q="", limit=100, offset=0):
        return {"records": self.records, "total": len(self.records)}

    def get_pages(self, run_id, limit=50):
        return self.pages

    def get_page(self, pid):
        return next((p for p in self.pages if p["id"] == pid), None)

    def update_record_cell(self, did, rid, field, cell):
        self.written.append((rid, field, cell))
        return True


def _run(coro):
    return asyncio.run(coro)


# --- plan recovery ----------------------------------------------------------
def test_the_plan_is_recovered_through_its_run():
    """dedupe_keys is the basis of identity and is not stored on the dataset.
    Recovering it by the same path the run used is the only way to be sure the
    backfill matches rows the way the deduper actually did."""
    plan = bf.plan_for_dataset(_Store(), "d1")
    assert plan["dedupe_keys"] == ["name"]


def test_a_dataset_with_no_run_is_refused():
    store = _Store(dataset={"id": "d1", "schema": []})
    with pytest.raises(bf.BackfillRefused, match="no run"):
        bf.plan_for_dataset(store, "d1")


def test_a_deleted_run_is_refused_rather_than_guessed():
    store = _Store()
    store.get_run = lambda rid: None
    with pytest.raises(bf.BackfillRefused, match="gone"):
        bf.plan_for_dataset(store, "d1")


# --- which fields -----------------------------------------------------------
def test_only_entirely_absent_fields_are_offered():
    store = _Store(records=[_rec("1", name=_cell("Acme"), funding=_cell("10M")),
                            _rec("2", name=_cell("Globex"))])
    matrix = {"fields": [{"field": "name", "present": 2},
                         {"field": "funding", "present": 1},
                         {"field": "employees", "present": 0}]}
    # `funding` is partial, `employees` is absent, `name` is full.
    assert bf.absent_fields(matrix) == ["employees"]


def test_requesting_a_partial_field_explicitly_yields_nothing():
    matrix = {"fields": [{"field": "funding", "present": 3}]}
    assert bf.absent_fields(matrix, ["funding"]) == []


# --- the proposal -----------------------------------------------------------
def test_a_proposal_reports_cost_and_pages_before_anything_runs():
    store = _Store(records=[_rec("1", name=_cell("Acme"))])
    out = bf.propose(store, "d1")
    assert out["fields"] == ["funding"]
    assert out["backfillable"] is True
    assert out["pages"] == 1
    assert out["estimated_extractions"] == 1
    # The dedupe key has to be extracted too, or nothing can be matched.
    assert "name" in out["extract_fields"] and "funding" in out["extract_fields"]
    assert out["reuses_stored_pages"] is True


def test_a_proposal_with_nothing_absent_says_so_instead_of_offering_work():
    store = _Store(records=[_rec("1", name=_cell("Acme"), funding=_cell("1M"))])
    out = bf.propose(store, "d1")
    assert out["backfillable"] is False
    assert out["fields"] == []
    assert "already has a value" in out["reason"]


def test_a_proposal_refuses_when_the_plan_has_no_identity_keys():
    store = _Store(records=[_rec("1", name=_cell("Acme"))], plan={
        "fields": [{"name": "name"}, {"name": "funding"}], "dedupe_keys": []})
    out = bf.propose(store, "d1")
    assert out["backfillable"] is False
    assert "no dedupe_keys" in out["reason"]


def test_a_proposal_with_no_stored_pages_says_there_is_nothing_to_read():
    store = _Store(records=[_rec("1", name=_cell("Acme"))], pages=[])
    out = bf.propose(store, "d1")
    assert out["backfillable"] is False
    assert "no readable pages" in out["reason"].lower()


def test_a_proposal_with_no_matchable_records_is_not_offered():
    store = _Store(records=[_rec("1", other=_cell("x"))])
    out = bf.propose(store, "d1")
    assert out["backfillable"] is False
    assert out["matchable_records"] == 0


# --- the negative identity cases -------------------------------------------
def test_a_backfill_without_dedupe_keys_writes_nothing(monkeypatch):
    """The dangerous shape: values that look right attached to the wrong row.
    Refusing is the only safe answer."""
    store = _Store(records=[_rec("1", name=_cell("Acme"))],
                   plan={"fields": [{"name": "name"}, {"name": "funding"}],
                         "dedupe_keys": []})

    async def _llm(prompt, schema):
        raise AssertionError("no LLM call should be made for an unserviceable request")

    with pytest.raises(bf.BackfillRefused, match="dedupe_keys"):
        _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert store.written == []


def test_a_similar_but_different_record_is_not_filled(monkeypatch):
    """The single most important test in this file.

    `Acme Corp` and `Acme Corporation` are the same company to the deduper's
    fuzzy tier, and that is correct for merging rows. Here they are different
    strings on different records, and filling the one from the other would
    attach a real quote to a real verdict on the wrong row — with nothing
    downstream able to tell.
    """
    store = _Store(records=[_rec("1", name=_cell("Acme Corp")),
                            _rec("2", name=_cell("Acme Corporation"))])
    raw = {"fields": {"name": "Acme Corporation", "funding": "$9M"},
           "evidence": [], "source_url": "https://x.example/", "source_title": "t",
           "source_text": "Acme Corporation raised $9M", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {n: _cell(r["fields"].get(n), "verified", "q", url, "p1")
                for n in ("name", "funding") if r["fields"].get(n)}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    out = _run(bf.run(store, "d1", llm=_llm, apply=True))
    # Exactly one record matched: the identical string. Never the near one.
    assert out["records_matched"] == 1
    assert len(store.written) == 1
    rid = store.written[0][0]
    assert rid == "2", f"wrote to the wrong record: {rid}"


def test_an_extraction_with_no_matching_record_is_reported_not_guessed(monkeypatch):
    store = _Store(records=[_rec("1", name=_cell("Acme"))])
    raw = {"fields": {"name": "Totally Different", "funding": "$9M"},
           "evidence": [], "source_url": "https://x.example/", "source_text": "x",
           "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell("$9M", "verified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    out = _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert store.written == [], "an unmatched extraction was written somewhere"
    assert out["unmatched"] == 1
    assert "no existing record" in out["unmatched_examples"][0]["reason"]


def test_a_record_with_no_identity_is_never_filled(monkeypatch):
    store = _Store(records=[_rec("1", name=_cell(""))])
    raw = {"fields": {"name": "Acme", "funding": "$9M"}, "evidence": [],
           "source_url": "u", "source_text": "x", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell("$9M", "verified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert store.written == [], "a record with no identity was filled anyway"


# --- never overwrite --------------------------------------------------------
def test_a_field_that_already_has_a_value_is_not_a_backfill_target(monkeypatch):
    """An existing value can never be overwritten, and the first line of defence
    is that the field is not a target at all.

    `funding` is present on record 1, so it is not "entirely absent" and is not
    offered. Backfill is scoped to gaps, not to disagreements — deciding that
    `$9M` beats an existing verified `$1M` is the deduper's job, and it does
    that by marking the cell conflicting so a human can look, not by overwriting
    in place. The per-cell guard in the service is a second line under this one.
    """
    store = _Store(records=[_rec("1", name=_cell("Acme"), funding=_cell("$1M", "verified"))])
    raw = {"fields": {"name": "Acme", "funding": "$9M"}, "evidence": [],
           "source_url": "u", "source_text": "x", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _llm(prompt, schema):
        raise AssertionError("no extraction should be attempted for a non-gap field")

    out = _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert store.written == []
    assert out["fillable"] == 0
    assert "no field is entirely absent" in out["reason"]


# --- dry run and apply ------------------------------------------------------
def test_a_dry_run_does_the_real_work_but_writes_nothing(monkeypatch):
    store = _Store(records=[_rec("1", name=_cell("Acme"))])
    raw = {"fields": {"name": "Acme", "funding": "$9M"}, "evidence": [],
           "source_url": "u", "source_text": "x", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell("$9M", "verified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    out = _run(bf.run(store, "d1", llm=_llm, apply=False))
    assert out["fillable"] == 1
    assert out["written"] == 0
    assert store.written == []
    assert out["applied"] is False


def test_apply_writes_exactly_what_a_dry_run_would_fill(monkeypatch):
    store = _Store(records=[_rec("1", name=_cell("Acme"))])
    raw = {"fields": {"name": "Acme", "funding": "$9M"}, "evidence": [],
           "source_url": "u", "source_text": "x", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell("$9M", "verified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    dry = _run(bf.run(store, "d1", llm=_llm, apply=False))
    wet = _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert dry["fillable"] == wet["fillable"] == 1
    assert wet["written"] == 1
    assert store.written[0][1] == "funding"


def test_two_pages_describing_the_same_record_fill_it_once(monkeypatch):
    store = _Store(records=[_rec("1", name=_cell("Acme"))],
                   pages=[{"id": "p1", "url": "u1", "markdown": "m", "raw_html": "h"},
                          {"id": "p2", "url": "u2", "markdown": "m", "raw_html": "h"}])

    async def _extract(reduced_plan, page, llm):
        return [{"fields": {"name": "Acme", "funding": "$9M"}, "evidence": [],
                 "source_url": page["url"], "source_text": "x",
                 "page_id": page["id"]}], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell("$9M", "verified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    out = _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert out["written"] == 1, f"the same record was filled {out['written']} times"


def test_an_empty_new_value_is_not_written(monkeypatch):
    """A fill that adds nothing is not a fill."""
    store = _Store(records=[_rec("1", name=_cell("Acme"))])
    raw = {"fields": {"name": "Acme", "funding": None}, "evidence": [],
           "source_url": "u", "source_text": "x", "page_id": "p1"}

    async def _extract(reduced_plan, page, llm):
        return [raw], "fake"

    monkeypatch.setattr(bf.extractor_svc, "extract_page", _extract)

    async def _wrap(specs, r, text, url, title, **kw):
        return {"funding": _cell(None, "unverified")}

    monkeypatch.setattr(bf.validator_svc, "wrap_record", _wrap)

    async def _llm(prompt, schema):
        return {}

    out = _run(bf.run(store, "d1", llm=_llm, apply=True))
    assert store.written == []
    assert out["fillable"] == 0
