"""What the API returns must agree with what the pipeline stored.

Every other suite in `tests/` calls service functions. Twelve files use
`TestClient` and they cover single routes. The defects fixed over the last five
commits were all *compositional* -- a page id stored and discarded, a refresh
that stored nothing, a click that promoted a cell whose offsets had been dropped,
a confidence frozen from a route name, a render failure erased into a silent
success -- and none is visible from one function in isolation. Each is visible
only when a record is written, stored, read back through the same routes the
console uses, and exported.

So this file drives the boundary: real store, real `wrap_record`, real
`finalize_dataset`, real routes, real exporters. The only thing replaced is the
search, the fetch and the judge -- at their service seams.

## What this deliberately does not do, and why

It does not drive `POST /api/runs` to completion.

That is not an oversight and it is worth recording what it took. The other twelve
HTTP suites avoid the problem by setting `OPENCODE_ENABLED = False`, which makes
the reader gate fail closed and the run finish "honestly empty" -- which is why
`test_api_p0` asserts `records == []`. Turning it on reaches the gate that
refuses an unreachable local reader, so the journey needs `opencode.available`
stubbed reachable (the seam `test_strict_reader._stub_state` uses) -- and with
that stubbed, the run still blocks on a network call further down that no
existing suite stubs.

So: **there is no hermetic test that runs a pipeline to completion through the
API.** `test_e2e_contract.py` covers the pipeline at service level, and this file
covers the read-back boundary. The gap between them is real and is recorded here
rather than papered over with a timeout that eventually fires.

## No test here asserts a record count

A test that fails when extraction finds one field fewer is a test that gets
deleted rather than fixed, and this suite has had three removed for exactly that.
What is asserted is *consistency*: that no two fields of a stored record
contradict each other, and that the evidence survives to the export.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core.config import settings as _settings  # noqa: E402


PAGE_TEXT = (
    "Acme Corp is a logistics company. "
    "Acme Corp was founded in 2011 by Jane Roe. "
    "Jane Roe is the chief executive of Acme Corp. "
    "Acme Corp is headquartered in Berlin."
)
PAGE_URL = "https://lists.example/acme"
PAGE_ID = "p-journey"

PLAN = {
    "entity": "Acme Corp",
    "goal": "find the chief executive of Acme Corp",
    "fields": [
        {"name": "company_name", "type": "string", "required": True,
         "description": "the company"},
        {"name": "ceo", "type": "string", "required": False,
         "description": "its chief executive"},
    ],
    "dedupe_keys": ["company_name"],
}

RAW = {
    "fields": {"company_name": "Acme Corp", "ceo": "Jane Roe"},
    "evidence": [
        {"field": "company_name", "value": "Acme Corp",
         "quote": "Acme Corp is a logistics company."},
        {"field": "ceo", "value": "Jane Roe",
         "quote": "Jane Roe is the chief executive of Acme Corp."},
    ],
}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """The app against a throwaway store.

    `chdir` before the client exists, because the lifespan builds the repository
    from the process CWD. That is the whole of `test_hermetic_stores.py`; without
    it these tests read and write the developer's real datasets.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    monkeypatch.setattr(_settings, "ALLOW_UNAUTHENTICATED", True)
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)

    from app.providers.decision import jev

    async def _yes(prompt, schema, **kw):
        return {"support": {"type": "noul", "noul": 0.9}}

    monkeypatch.setattr(jev, "_jev_call", _yes)

    with TestClient(main_mod.app) as client:
        yield client


def _wrap():
    """The real gate, on the real page text, with a reachable judge."""
    from app.services import validator as validator_svc

    return asyncio.run(validator_svc.wrap_record(
        PLAN["fields"], RAW, PAGE_TEXT, PAGE_URL, "Acme", "now",
        page_id=PAGE_ID,
        budget=validator_svc.JudgeBudget(0, unlimited=True)))


def _finalize(**counts):
    c = {"attempted": 1, "successful": 1, "failed": 0, "skipped": 0,
         "records": 1, "verified": 2, "needs_review": 0}
    c.update(counts)
    return main_mod.REPO.finalize_dataset(
        "run-journey", PLAN, [{"fields": _wrap()}], c)


# --- the read-back boundary ---------------------------------------------------

def test_a_stored_record_read_through_the_api_agrees_with_its_evidence(app_client):
    """The Stage C invariant, at the edge a user reads.

    Not "wrap_record returns verified" -- there are thirty unit tests for that.
    This reads a stored record back over HTTP and checks that no two fields of it
    contradict each other: a status, a locator, a confidence and a quote that all
    describe the same claim.
    """
    did = _finalize()
    body = app_client.get(f"/api/datasets/{did}?include_records=true").json()
    records = body["data"]["records"]
    assert records, "the record was stored but is not readable back"

    from app.schemas.evidence import locate_quote

    for name, cell in records[0]["fields"].items():
        status = cell.get("verification_status")
        src = cell.get("source") or {}

        if status == "verified":
            assert src.get("start") is not None, (
                f"{name} is verified with no offsets, so its citation cannot be "
                "checked by anyone including the product's own proof view")
            quote = src.get("quote", "")
            assert locate_quote(quote, PAGE_TEXT)[0] is not None, (
                f"{name}'s quote does not appear in the page it cites")
            assert PAGE_TEXT[src["start"]:src["end"]] == quote, (
                f"{name}'s recorded offsets do not address its own quote")
            # Not "sure": the prior is per route, and an `llm` read scores 0.75.
            # What must hold is that a checked claim scores something and an
            # unchecked one scores nothing -- the gate is absolute, not a discount.
            assert cell.get("confidence", 0) > 0, (
                f"{name} is verified but carries no confidence at all")
        else:
            assert src.get("start") is None, (
                f"{name} is {status} yet presents a locator")
            assert cell.get("confidence") == 0.0, (
                f"{name} is {status} but carries confidence "
                f"{cell.get('confidence')}; an unchecked claim scores nothing")
            assert cell.get("certainty") != "sure", (
                f"{name} is {status} but renders as 'sure' to the reader")


def test_a_refused_value_survives_the_round_trip_as_refused(app_client):
    """The other direction, and the one that regressed twice.

    A quote that is not on the page must come back not-verified *and* without
    offsets. It used to come back `verified` with `start: None`, which is a claim
    nobody can check.
    """
    from app.services import validator as validator_svc

    bad = {"fields": {"company_name": "Acme Corp", "ceo": "Globex"},
           "evidence": [
               {"field": "company_name", "value": "Acme Corp",
                "quote": "Acme Corp is a logistics company."},
               {"field": "ceo", "value": "Globex",
                "quote": "Globex is a chemicals concern in entirely another city."},
           ]}
    wrapped = asyncio.run(validator_svc.wrap_record(
        PLAN["fields"], bad, PAGE_TEXT, PAGE_URL, "Acme", "now", page_id=PAGE_ID,
        budget=validator_svc.JudgeBudget(0, unlimited=True)))
    did = main_mod.REPO.finalize_dataset(
        "run-journey", PLAN, [{"fields": wrapped}],
        {"attempted": 1, "successful": 1, "failed": 0, "skipped": 0,
         "records": 1, "verified": 1, "needs_review": 1})

    records = app_client.get(
        f"/api/datasets/{did}?include_records=true").json()["data"]["records"]
    ceo = records[0]["fields"]["ceo"]
    assert ceo["verification_status"] != "verified"
    assert (ceo.get("source") or {}).get("start") is None
    assert ceo.get("certainty") != "sure"


def test_the_export_carries_the_evidence_not_just_the_values(app_client):
    """An export that drops the quotes has lost the dataset's entire claim."""
    did = _finalize()
    r = app_client.post(f"/api/datasets/{did}/export", json={"format": "json"})
    assert r.status_code == 200, r.text
    assert "Acme Corp is a logistics company." in r.text, (
        "the quote did not survive the export")
    assert "lists.example" in r.text, (
        "the export names no source, so a reader cannot go and check anything")


def test_the_csv_export_carries_the_source_url(app_client):
    did = _finalize()
    r = app_client.post(f"/api/datasets/{did}/export", json={"format": "csv"})
    assert r.status_code == 200
    assert "lists.example" in r.text
    assert "Acme Corp" in r.text


def test_a_stored_page_records_its_fetch_outcome(app_client):
    """The Stage E contract, read back rather than asserted at the service."""
    repo = main_mod.REPO
    pid = repo.upsert_page("run-journey", {
        "url": PAGE_URL, "markdown": PAGE_TEXT, "raw_html": "",
        "content_hash": "h1", "snapshot_chars": len(PAGE_TEXT),
        "status": "ok", "render_attempted": False, "render_status": "not_needed",
        "render_error_code": "", "fallback_used": False,
        "final_content_source": "http",
        "fetch_attempts": [{"method": "http", "outcome": "ok", "error_code": ""}],
        "reducer_version": "1"})

    page = repo.get_page(pid)
    assert page["render_status"] == "not_needed"
    assert page["fallback_used"] is False
    assert page["final_content_source"] == "http"
    assert page["fetch_attempts"], "the attempt history must survive the round trip"

    served = app_client.get(f"/api/pages/{pid}")
    assert served.status_code == 200


def test_the_records_route_agrees_with_the_dataset_route(app_client):
    """Two routes, one record.

    `/records` paginates and the dataset route does not. If they disagree about
    what a record is, the console shows two different datasets for one run.
    """
    did = _finalize()
    full = app_client.get(f"/api/datasets/{did}?include_records=true").json()
    page = app_client.get(f"/api/datasets/{did}/records").json()
    a = full["data"]["records"]
    b = page["data"]["records"]
    assert len(a) == len(b), "the two routes disagree about the record count"
    assert a[0]["fields"].keys() == b[0]["fields"].keys()


def test_an_unknown_dataset_is_404_from_every_route_that_names_it(app_client):
    """Typos must not read as empty datasets.

    An empty dataset and a missing one are different answers, and a UI cannot tell
    them apart if both are `200` with `records: []`.
    """
    missing = "00000000-0000-4000-8000-000000000000"
    for url in ("/api/datasets/nope", "/api/datasets/nope/records",
                "/api/datasets/nope/sources", f"/api/pages/{missing}"):
        r = app_client.get(url)
        assert r.status_code == 404, f"{url} answered {r.status_code}"


def test_a_malformed_id_is_422_and_not_503(app_client):
    """Distinct from "missing", and pinned by `test_uuid_guard`.

    Worth restating here because it is the boundary this file is about: three
    different answers -- malformed, missing, and an error -- must not collapse
    into one.
    """
    assert app_client.get("/api/pages/not-a-uuid").status_code == 422