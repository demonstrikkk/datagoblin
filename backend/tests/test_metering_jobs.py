"""Phase-5 tests: metering math, idempotent ledger, budgets, job status, map.

Hermetic: no network, no keys. API tests reuse the P0 TestClient pattern.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")

from app.services import jobs as jobs_svc  # noqa: E402
from app.services import metering as metering_svc  # noqa: E402
from app.services import runner as runner_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# -- price table + projection ------------------------------------------------------
def test_project_cost_math():
    proj = metering_svc.project_cost({"search_queries": ["a", "b"], "max_pages": 12})
    assert proj["queries"] == 2 and proj["pages"] == 12
    assert proj["breakdown"] == {"discover_query": 2, "fetch_extract_pages": 36,
                                 "judge_call": 0}
    assert proj["total"] == 38
    assert metering_svc.charge_id("j", "fetch_page") == "j:fetch_page"
    assert metering_svc.charge_id("j", "export", 2) == "j:export:2"


# -- ledger idempotency -----------------------------------------------------------------
def test_ledger_idempotent():
    ledger = metering_svc.Ledger()
    first = ledger.bill("j1", "fetch_page", 3)
    assert (first["credits"], first["billed"]) == (3, True)
    again = ledger.bill("j1", "fetch_page", 3)
    assert again["billed"] is False and ledger.total("j1") == 3
    assert ledger.total("other") == 0
    assert len(ledger.for_job("j1")) == 1


# -- meter budgets --------------------------------------------------------------------------
def test_meter_unlimited_by_default():
    meter = metering_svc.Meter("j")
    assert meter.unlimited and meter.remaining() is None
    assert meter.spend("fetch_page", 100) is not None
    assert meter.clamp_pages(12) == 12


def test_meter_enforces_and_clamps():
    meter = metering_svc.Meter("j", 10)
    assert meter.clamp_pages(12) == 3  # (10 - 1 discover) // 3 per page
    assert meter.spend("discover_query", 1) is not None  # spent 1
    assert meter.spend("fetch_page", 3) is not None  # spent 4
    assert meter.spend("extract_page", 3) is not None  # 4 + 6 == 10: exact fit OK
    assert meter.exhausted() is True and meter.remaining() == 0
    assert meter.spend("fetch_page", 1) is None  # over budget: refused
    assert meter.spend("extract_page", 0) is not None  # deterministic: free
    summary = meter.summary()
    assert summary["spent"] == 10 and summary["budget"] == 10


# -- local repo persistence ----------------------------------------------------------------------
def test_local_repo_ledger_and_fingerprints(tmp_path):
    from app.repositories.local_repo import LocalRepo
    repo = LocalRepo(root=str(tmp_path / "ldb"))
    repo.record_charge("r1", {"charge_id": "r1:fetch_page", "stage": "fetch_page",
                              "units": 2, "credits": 2})
    assert repo.ledger("r1")[0]["credits"] == 2
    assert repo.ledger("nope") == []
    assert repo.seen_fingerprint("abc") is False
    repo.note_fingerprint("r1", "abc", "https://x.example/a")
    assert repo.seen_fingerprint("abc") is True
    repo.note_fingerprint("r1", "", "https://x.example/a")  # empty: no-op


# -- job status ----------------------------------------------------------------------------
def _finalized_repo(tmp_path):
    from app.repositories.local_repo import LocalRepo
    repo = LocalRepo(root=str(tmp_path / "ldb"))
    repo.create_run("r1", "w1", {"goal": "g"})
    rows = [{"fields": {"a": {"value": i, "verification_status": "verified",
                              "source": {}}}} for i in range(5)]
    did = repo.finalize_dataset("r1", {"goal": "g", "fields": []}, rows,
                                {"attempted": 2, "successful": 2, "failed": 0})
    repo.record_charge("r1", {"charge_id": "r1:fetch_page", "stage": "fetch_page",
                              "units": 2, "credits": 2})
    return repo, did


def test_job_status_completed(tmp_path):
    repo, did = _finalized_repo(tmp_path)
    st = jobs_svc.get_job_status(repo, "r1", skip=0, limit=2, memory={"status": "COMPLETED",
                                                                     "dataset_id": did})
    assert st["status"] == "COMPLETED" and st["completed"] == 5 and st["total"] == 5
    assert st["creditsUsed"] == 2 and len(st["data"]) == 2
    assert st["next"] == "/api/jobs/r1?skip=2&limit=2"
    last = jobs_svc.get_job_status(repo, "r1", skip=4, limit=2,
                                   memory={"status": "COMPLETED", "dataset_id": did})
    assert len(last["data"]) == 1 and last["next"] == ""


def test_job_status_running_and_unknown(tmp_path):
    from app.repositories.local_repo import LocalRepo
    repo = LocalRepo(root=str(tmp_path / "ldb"))
    running = jobs_svc.get_job_status(repo, "r9", memory={"status": "FETCHING",
                                                          "counters": {"attempted": 4}})
    assert running["status"] == "FETCHING" and running["total"] == 4
    assert running["completed"] == 0 and running["data"] == []
    repo.create_run("r8", "w8", {})
    stored = jobs_svc.get_job_status(repo, "r8")
    assert stored["status"] == "DISCOVERING"
    assert jobs_svc.get_job_status(repo, "ghost") is None


# -- runner budget wiring ---------------------------------------------------------------------------
def _plan(**over):
    base = {"goal": "g", "entity": "Acme", "requested_count": 2, "max_results": 2,
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "search_queries": ["Acme"], "seed_domains": [],
            "seed_urls": [f"https://x.example/{i}" for i in range(4)],
            "source_types": [], "traversal": {"max_pages_per_domain": 1},
            "validation_rules": [], "dedupe_keys": ["company_name"],
            "allowed_sources": [], "max_pages": 12}
    base.update(over)
    return base


def _ctx(events, charges):
    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        return {"url": url, "title": "T",
                "html": f"<html><body><p>Acme AI {url[-6:]}</p></body></html>",
                "method": "http"}

    async def _llm(prompt, schema):
        return {"data": {"records": [
            {"fields": {"company_name": "Acme AI"},
             "evidence": [{"field": "company_name", "quote": "Acme AI",
                           "source_url": "https://x.example/"}]}],
            "coverage": "full"}, "provider": "test"}

    async def _store(rid, pl, rows, counts):
        return "d1"

    async def _persist(s):
        pass

    def _record_charge(entry):
        charges.append(entry)

    async def _emit(ev):
        events.append(ev)

    ctx = {"search": _search, "fetch": _fetch, "llm": _llm, "store": _store,
           "persist_source": _persist, "record_charge": _record_charge,
           "cancelled": lambda: False}
    return ctx, _emit


def test_runner_bills_actuals_unlimited():
    events: list = []
    charges: list = []
    ctx, emit = _ctx(events, charges)
    result = run(runner_svc.execute_run("r1", _plan(), ctx, emit))
    assert result["status"] == "COMPLETED"
    clamp_notes = [e for e in events if "budget clamp" in e.get("message", "")]
    assert len(clamp_notes) == 0  # unlimited default: no clamp
    stages = {c["stage"] for c in charges}
    assert stages == {"discover_query", "fetch_page", "extract_page", "judge_call"}
    assert sum(c["credits"] for c in charges) == 1 + 4 + 8 + 0  # 1q + 4 pages + 4x2 + 0 conflicts


def test_runner_budget_clamp_and_fail_fast():
    events: list = []
    charges: list = []
    ctx, emit = _ctx(events, charges)
    plan = _plan()
    plan["credit_budget"] = 10  # discover 1 + 3 pages x (1+2)
    result = run(runner_svc.execute_run("r1", plan, ctx, emit))
    assert result["status"] == "COMPLETED"
    assert any("budget clamp" in e.get("message", "") for e in events)

    events2: list = []
    charges2: list = []
    ctx2, emit2 = _ctx(events2, charges2)
    poor = _plan()
    poor["credit_budget"] = 1  # cannot afford a single page
    result2 = run(runner_svc.execute_run("r2", poor, ctx2, emit2))
    assert result2["status"] == "FAILED" and "budget" in result2["error"]


# -- API: jobs + map ----------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    from fastapi.testclient import TestClient
    from app import main as main_mod
    with TestClient(main_mod.app) as c:
        yield c


def _compile_and_run(client):
    plan_id = client.post("/api/workflows/compile",
                          json={"prompt": "Find Acme"}).json()["data"]["plan_id"]
    run_id = client.post("/api/runs", json={"plan_id": plan_id}).json()["data"]["run_id"]
    import time
    end = time.monotonic() + 20.0
    while time.monotonic() < end:
        st = client.get(f"/api/runs/{run_id}").json()["data"]
        if st["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return run_id, st
        time.sleep(0.2)
    raise AssertionError("run never finished")


def test_jobs_endpoint_after_offline_run(client):
    run_id, st = _compile_and_run(client)
    assert st["status"] == "COMPLETED"
    job = client.get(f"/api/jobs/{run_id}").json()["data"]
    assert job["status"] == "COMPLETED" and job["id"] == run_id
    assert job["creditsUsed"] >= 0 and job["data"] == []
    assert client.get("/api/jobs/ghost").status_code == 404


def test_map_endpoint_bills_one_credit(client, monkeypatch):
    from app import main as main_mod

    async def _fake_fetch(url, method):
        return {"url": url, "final_url": url, "title": "T",
                "html": '<a href="/a">a</a><a href="https://other.example/b">b</a>'
                        '<a href="mailto:x@y">c</a>',
                "method": "http"}

    monkeypatch.setattr(main_mod, "_fetch_method", _fake_fetch)
    r = client.post("/api/map", json={"url": "https://x.example/"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["links"] == ["https://x.example/a", "https://other.example/b"]
    assert data["credits_used"] == 1
    bad = client.post("/api/map", json={"url": "not a url"})
    assert bad.status_code == 422
