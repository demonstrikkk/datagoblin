"""A cell must cite a page that exists.

Three paths wrote evidence whose `page_id` was `""`:

  * `gapfill` called `persist_page` for every page it fetched, collected the
    returned ids into `page_ids[i]`, and then never read the map. The page was
    stored -- the code says so in a comment, and the reason is right -- and the
    id it was stored under was thrown away.
  * `refresh` had no way to store anything. `run()` took no `persist_page`, the
    route passed none, and every cell it replaced was written with `page_id: ""`.
  * `backfill` shares the same extraction path as the run, which does persist,
    so it is the control: it is the only one of the three that was right.

The failure is not cosmetic in either service. An empty `page_id` is what the UI
shows as "source unknown", and the quote carried beside it cannot be checked
against anything -- a stored dataset whose evidence points nowhere is
indistinguishable from one with no evidence, which is precisely the state
`judgment_unavailable` was invented to distinguish.

Refresh is the worse of the two because it is the only path that *replaces* an
existing value. It overwrote a good cell whose evidence resolved, and put in its
place a new value citing text that existed nowhere.

Each test below is written to fail against the pre-fix code, and asserts the id
that actually landed in the cell rather than that `persist_page` was called --
calling it and discarding the result is exactly the defect.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import gapfill as F  # noqa: E402
from app.services import gaps as G  # noqa: E402
from app.services import refresh as refresh_svc  # noqa: E402


PAGE_TEXT = ("Acme Corp was founded in 2011 by Jane Roe. "
             "Jane Roe is the chief executive of Acme Corp.")

PLAN = {"entity": "Acme Corp", "goal": "acme ceo",
        "fields": [{"name": "ceo", "type": "string", "required": False},
                   {"name": "company_name", "type": "string", "required": False}],
        "dedupe_keys": ["company_name"]}


def _record():
    return {"record_id": "r1",
            "fields": {"company_name": {"value": "Acme Corp",
                                        "verification_status": "verified"}}}


class _Store:
    """Enough of the repository for the real extraction and write paths."""

    def __init__(self):
        self.written: list[tuple] = []
        self.stored: list[dict] = []

    def get_gap(self, dataset_id, field):
        return {"state": "open", "category": "depth_gap",
                "phase": G.PHASE_STORED, "attempts": 1, "field": field}

    def get_dataset_row(self, dataset_id):
        return {"id": dataset_id, "run_id": "run1", "schema": PLAN["fields"]}

    def get_run(self, run_id):
        return {"id": run_id, "workflow_id": "wf1"}

    def get_plan(self, workflow_id):
        return PLAN

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        return {"records": [_record()]}

    def update_record_cell(self, dataset_id, record_id, field, cell):
        self.written.append((record_id, field, cell))
        return True

    # The store side of the contract: a page must become a row.
    async def persist(self, page):
        self.stored.append(page)
        return f"page-{len(self.stored)}"

    def get_page(self, page_id):
        return None

    def get_sources(self, dataset_id):
        return {"sources": [{"url": "https://lists.example/acme-corp",
                             "status": "ok", "title": "Acme"}]}


def _page(url="https://lists.example/acme-corp"):
    return {"url": url, "markdown": PAGE_TEXT, "html": "",
            "content_hash": "h-acme", "status": "ok"}


def _llm(prompt, schema):
    """An ExtractionEnvelope: `{"records": [...]}`, not a bare field map."""
    async def _go():
        return {"coverage": "partial",
                "records": [{
                    # company_name is the dedupe key, so an extraction that
                    # omits it cannot be attributed to a record at all.
                    "fields": {"company_name": "Acme Corp", "ceo": "Jane Roe"},
                    "evidence": [{
                        "field": "company_name", "value": "Acme Corp",
                        "quote": "Acme Corp was founded in 2011 by Jane Roe.",
                    }, {
                        "field": "ceo", "value": "Jane Roe",
                        "quote": "Jane Roe is the chief executive of Acme Corp.",
                    }],
                }]}
    return _go()


# --- gapfill ------------------------------------------------------------------

def _run_gapfill(store, apply=True):
    async def _search(query, limit=4):
        return [{"url": "https://lists.example/acme-corp", "title": "Acme"}]

    async def _fetch(url, method):
        return _page(url)

    return asyncio.run(F.run(
        store, "d1", "ceo", phase=G.PHASE_SEARCH, llm=_llm,
        search_fn=_search, fetch_fn=_fetch, persist_page=store.persist,
        apply=apply))


def test_gapfill_writes_the_page_id_it_stored_the_page_under():
    store = _Store()
    _run_gapfill(store)

    assert store.stored, "nothing was stored, so the test proves nothing"
    assert store.written, "no cell was written, so the test proves nothing"

    _rid, _field, cell = store.written[0]
    assert cell["source"].get("page_id"), (
        "the page was stored and its id discarded: the map that carried it was "
        "built in the loop above and never read again")


def test_gapfill_cell_ids_name_a_stored_page():
    """Not just non-empty -- it must be the id the store actually assigned."""
    store = _Store()
    _run_gapfill(store)

    assert store.stored, "nothing stored"
    for _rid, _field, cell in store.written:
        assert cell["source"].get("page_id") == "page-1", (
            "the cell cites an id that is not the one the page was stored "
            "under, so it is not merely empty, it is wrong")


def test_gapfill_without_a_persist_hook_still_refuses_to_invent_an_id():
    """Fails closed. `persist_page=None` must leave the field empty rather than
    fabricate something, because an invented id resolves to nobody's page."""
    store = _Store()

    async def _search(query, limit=4):
        return [{"url": "https://lists.example/acme-corp", "title": "Acme"}]

    async def _fetch(url, method):
        return _page(url)

    asyncio.run(F.run(store, "d1", "ceo", phase=G.PHASE_SEARCH, llm=_llm,
                      search_fn=_search, fetch_fn=_fetch, persist_page=None,
                      apply=True))
    for _rid, _field, cell in store.written:
        assert not cell["source"].get("page_id")


# --- refresh ------------------------------------------------------------------

def _run_refresh(store, apply=True, persist=True):
    async def _fetch(url):
        return _page(url)

    out = asyncio.run(refresh_svc.run(
        store, "d1", ["lists.example"], llm=_llm, fetch=_fetch, apply=apply,
        persist_page=store.persist if persist else None))
    return out


def _refresh_store():
    store = _Store()

    def get_records(dataset_id, q="", limit=100, offset=0):
        rec = _record()
        # A *stale* value, not an empty one. Refresh refuses to fill empty cells
        # -- that is backfill's job -- so it only writes when it disagrees with
        # something already there, which is also the case where the page_id
        # matters most: it replaces a cell whose evidence resolved.
        rec["fields"]["ceo"] = {"value": "John Doe",
                                "verification_status": "verified",
                                "source": {"url": "https://lists.example/acme-corp",
                                           "quote": "John Doe was the chief executive of Acme Corp.",
                                           "retrieved_at": "then",
                                           "page_id": "page-old",
                                           "start": 0, "end": 10}}
        return {"records": [rec]}

    store.get_records = get_records
    return store


def test_refresh_persists_the_page_it_re_reads():
    """Refresh has no crawl queue, so persisting is its own responsibility.

    It had no `persist_page` parameter at all, so a replacement value was
    written citing `page_id: ""` -- an unreferenceable quote, on the one path
    that overwrites a value whose evidence previously resolved.
    """
    store = _refresh_store()
    _run_refresh(store)

    assert store.stored, (
        "refresh re-read a page and stored nothing, so its quotes cannot be "
        "checked against anything")


def test_refresh_writes_the_page_id_under_which_it_stored_the_page():
    store = _refresh_store()
    _run_refresh(store)

    assert store.stored, "nothing stored"
    assert store.written, "no cell changed, so the test proves nothing"
    for _rid, _field, cell in store.written:
        assert cell["source"].get("page_id") == "page-1", (
            "a refreshed value cites an id the reader cannot follow")