"""P0 checkpoint: real API end-to-end offline (TestClient, LocalRepo, no keys/network).

Path verified: compile -> runs -> status/SSE -> history -> dataset -> records
-> sources -> export(JSON+CSV). Live LLM/Tavily absent: Tavily raises no-key,
seeds absent, run honestly completes empty; a repo-finalized dataset proves the
read/export path with real rows.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    # Offline, stated rather than arranged. See the same note in
    # test_p1_contracts.py: `chdir` used to hide .env and silently disable the
    # provider, which stopped being true once config resolves .env
    # absolutely. setattr, not setenv: `settings` is a singleton built at
    # import, so an env var set afterwards is never read.
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
    with TestClient(main_mod.app) as c:
        yield c


def _compile(client, prompt="Find 15 AI startups in London with founders and funding"):
    r = client.post("/api/workflows/compile", json={"prompt": prompt})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None and body["data"]["plan_id"] and body["data"]["plan"]["entity"]
    return body["data"]


def _wait_terminal(client, run_id, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get(f"/api/runs/{run_id}").json()["data"]
        if st["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never reached terminal state")


def test_full_path_offline(client):
    compiled = _compile(client)
    r = client.post("/api/runs", json={"plan_id": compiled["plan_id"]})
    assert r.status_code == 200
    run_id = r.json()["data"]["run_id"]  # navigable ID (frontend regression)
    assert run_id

    st = _wait_terminal(client, run_id)
    assert st["status"] == "COMPLETED"  # honest empty: no keys, no seeds
    assert st["dataset_id"]

    stream = client.get(f"/api/runs/{run_id}/stream")
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    assert "run.completed" in stream.text

    hist = client.get("/api/history").json()["data"]
    assert any(h.get("id") == run_id or h.get("run_id") == run_id for h in hist)

    # Records are opt-in: the page fetches them from /records, and shipping
    # the whole dataset here as well doubled every dataset page load.
    lean = client.get(f"/api/datasets/{st['dataset_id']}").json()["data"]
    assert "records" not in lean
    ds = client.get(f"/api/datasets/{st['dataset_id']}?include_records=true").json()["data"]
    assert ds["id"] == st["dataset_id"] and ds["records"] == []

    recs = client.get(f"/api/datasets/{st['dataset_id']}/records").json()["data"]
    assert recs["total"] == 0

    srcs = client.get(f"/api/datasets/{st['dataset_id']}/sources").json()["data"]
    assert srcs["dataset_id"] == st["dataset_id"]


def test_read_export_path_with_real_row(client):
    compiled = _compile(client)
    raw = {"fields": {"company_name": "Acme AI"},
           "evidence": [{"field": "company_name", "quote": "Acme AI raises",
                         "source_url": "https://s.example/a"}]}
    wrapped = asyncio.run(validator_svc.wrap_record(
        compiled["plan"]["fields"][:1], raw, "Acme AI raises seed today",
        "https://s.example/a", "T"))
    did = main_mod.REPO.finalize_dataset(
        "run-x", compiled["plan"], [{"fields": wrapped}],
        {"attempted": 1, "successful": 1, "failed": 0, "skipped": 0,
         "records": 1, "verified": 1, "needs_review": 0})

    ds = client.get(f"/api/datasets/{did}?include_records=true").json()["data"]
    assert len(ds["records"]) == 1
    assert ds["records"][0]["fields"]["company_name"]["value"] == "Acme AI"

    csv_r = client.post(f"/api/datasets/{did}/export", json={"format": "csv"})
    assert csv_r.status_code == 200
    assert csv_r.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_r.headers.get("content-disposition", "")
    assert "Acme AI" in csv_r.text

    js = client.post(f"/api/datasets/{did}/export", json={"format": "json"}).json()
    assert "Acme AI" in js["data"]["content"]


def test_empty_export_is_422_not_silent(client):
    compiled = _compile(client)
    r = client.post("/api/runs", json={"plan_id": compiled["plan_id"]})
    run_id = r.json()["data"]["run_id"]
    st = _wait_terminal(client, run_id)
    r = client.post(f"/api/datasets/{st['dataset_id']}/export", json={"format": "csv"})
    assert r.status_code == 422  # honest: nothing to export


def test_unknown_ids_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404
    assert client.get("/api/datasets/does-not-exist").status_code == 404
    assert client.post("/api/runs/does-not-exist/cancel").status_code == 404
    assert client.post("/api/runs", json={"plan_id": "nope"}).status_code == 422


def test_registry_eviction_bounds():
    main_mod.RUNS.clear()
    main_mod.TASKS.clear()
    main_mod.PLANS.clear()
    for i in range(205):
        main_mod.RUNS[f"r-{i}"] = {"status": "COMPLETED"}
    main_mod.RUNS["active-1"] = {"status": "DISCOVERING"}
    main_mod._evict_registries()
    assert len(main_mod.RUNS) <= 200
    assert "active-1" in main_mod.RUNS  # active never evicted
    main_mod.RUNS.clear()
