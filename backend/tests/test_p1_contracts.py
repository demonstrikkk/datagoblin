"""P1 contracts: batched finalize, string seeds, records bounds, empty-sig dedupe,
semantic fail-fast, real RunView counters. No network/keys (supabase faked at seam)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core import config as config_mod  # noqa: E402
from app.services import deduper as deduper_svc  # noqa: E402


class _Table:
    def __init__(self, rec, name):
        self._rec, self._name = rec, name

    def insert(self, payload):
        self._rec.calls.append((self._name, "insert", payload))
        return self

    def upsert(self, payload, on_conflict=None):
        self._rec.calls.append((self._name, "upsert", payload))
        return self

    def update(self, payload):
        self._rec.calls.append((self._name, "update", payload))
        return self

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        self._rec.calls.append((self._name, "execute", None))

        class _R:
            data = []
        return _R()


class _FakeDB:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _Table(self, name)


def test_finalize_single_bulk_insert():
    from app.repositories.supabase_repo import SupabaseRepo
    repo = SupabaseRepo.__new__(SupabaseRepo)
    repo._db = _FakeDB()
    rows = [{"fields": {"a": {"value": i}}} for i in range(5)]
    repo.finalize_dataset("r1", {"goal": "g", "fields": []}, rows, {})
    rec_inserts = [c for c in repo._db.calls if c[0] == "dataset_records" and c[1] == "insert"]
    assert len(rec_inserts) == 1 and len(rec_inserts[0][2]) == 5


def test_semantic_flag_fails_before_work(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "FEATURE_SEMANTIC_DEDUP", True)
    with pytest.raises(RuntimeError):
        deduper_svc.dedupe_records([{"fields": {"a": {"value": "x"}}}], ["a"])
    assert config_mod.settings.FEATURE_SEMANTIC_DEDUP is True  # unchanged by failure


def test_empty_signature_never_merges():
    rows = [{"fields": {"company_name": {"value": None, "verification_status": "unverified",
                                         "source": {}}}},
            {"fields": {"company_name": {"value": "", "verification_status": "unverified",
                                         "source": {}}}}]
    canon, merged = deduper_svc.dedupe_records(rows, ["company_name"])
    assert len(canon) == 2 and merged == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    with TestClient(main_mod.app) as c:
        yield c


def test_compile_normalizes_seeds_to_strings(client):
    r = client.post("/api/workflows/compile",
                    json={"prompt": "Find startups",
                          "seed_urls": [{"url": "https://s.example/a", "title": "T",
                                         "file": "x.html"},
                                        "https://s.example/b", 42, None]})
    assert r.status_code == 200
    seeds = r.json()["data"]["plan"]["seed_urls"]
    assert seeds == ["https://s.example/a", "https://s.example/b"]


def test_records_bounds_rejected(client):
    r = client.post("/api/workflows/compile", json={"prompt": "Find startups"})
    pid = r.json()["data"]["plan_id"]
    run_id = client.post("/api/runs", json={"plan_id": pid}).json()["data"]["run_id"]
    import time
    end = time.monotonic() + 20
    while client.get(f"/api/runs/{run_id}").json()["data"]["status"] not in (
            "COMPLETED", "FAILED", "CANCELLED") and time.monotonic() < end:
        time.sleep(0.2)
    did = client.get(f"/api/runs/{run_id}").json()["data"]["dataset_id"]
    assert client.get(f"/api/datasets/{did}/records?limit=0").status_code == 422
    assert client.get(f"/api/datasets/{did}/records?offset=-1").status_code == 422
    assert client.get(f"/api/datasets/{did}/records?limit=501").status_code == 422
    assert client.get(f"/api/datasets/{did}/records?limit=10&offset=0").status_code == 200


def test_runview_counters_real(client):
    import time
    r = client.post("/api/workflows/compile", json={"prompt": "Find startups"})
    run_id = client.post("/api/runs",
                         json={"plan_id": r.json()["data"]["plan_id"]}).json()["data"]["run_id"]
    end = time.monotonic() + 20
    while True:
        st = client.get(f"/api/runs/{run_id}").json()["data"]
        if st["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        assert time.monotonic() < end
        time.sleep(0.2)
    assert set(st["counters"]) == {"attempted", "successful", "failed",
                                   "records", "verified", "needs_review"}
