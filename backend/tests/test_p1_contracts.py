"""P1 contracts: batched finalize, string seeds, records bounds, empty-sig dedupe,
semantic fail-fast, real RunView counters. No network/keys (the DB is faked at
the connection seam, so the SQL and the transaction shape are still asserted)."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core import config as config_mod  # noqa: E402
from app.services import deduper as deduper_svc  # noqa: E402


class _CopySink:
    def __init__(self, calls):
        self._calls = calls
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write_row(self, row):
        self.rows.append(row)


class _FakeCursor:
    def __init__(self, calls):
        self._calls = calls
        self._copy = None

    description = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self._calls.append(("execute", " ".join(sql.split())[:70], params))
        return self

    def copy(self, sql):
        self._calls.append(("copy", " ".join(sql.split())[:70], ()))
        self._copy = _CopySink(self._calls)
        return self._copy

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls
        self.cur = _FakeCursor(calls)
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def test_finalize_writes_all_records_in_one_copy():
    """Records must be written as a single batch inside one transaction.

    The REST adapter this replaced issued one request per table and could
    leave a dataset written without its run status; the property that matters
    is that N records cost one COPY, not N round trips, and that the run is
    marked complete in the same transaction.
    """
    from app.repositories.postgres_repo import PostgresRepo
    calls: list = []
    repo = PostgresRepo.__new__(PostgresRepo)
    repo._dsn = "postgresql://unused"
    repo._lock = threading.Lock()
    conn = _FakeConn(calls)
    repo._conn = lambda: conn

    rows = [{"fields": {"a": {"value": i}}} for i in range(5)]
    did = repo.finalize_dataset(
        "11111111-1111-1111-1111-111111111111",
        {"goal": "g", "fields": []}, rows, {})

    copies = [c for c in calls if c[0] == "copy"]
    assert len(copies) == 1, f"expected exactly one COPY, got {len(copies)}"
    assert len(conn.cur._copy.rows) == 5
    assert all(r[0] == did for r in conn.cur._copy.rows)
    # Terminal run state lands in the same transaction, so a run is never
    # marked complete without its records.
    assert any("UPDATE runs SET status=%s" in c[1] for c in calls
               if c[0] == "execute")
    assert conn.commits == 1


def test_partial_run_is_never_marked_completed():
    """A run that hit its budget is FAILED with partial=true. Marking a
    truncated run COMPLETED because a dataset happened to be written is how a
    15-record run came to look finished."""
    from app.repositories.postgres_repo import PostgresRepo
    calls: list = []
    repo = PostgresRepo.__new__(PostgresRepo)
    repo._dsn = "postgresql://unused"
    repo._lock = threading.Lock()
    conn = _FakeConn(calls)
    repo._conn = lambda: conn
    repo.finalize_dataset(
        "11111111-1111-1111-1111-111111111111", {"goal": "g", "fields": []},
        [{"fields": {"a": {"value": 1}}}],
        {"partial": True, "partial_reason": "runtime budget exhausted after 2 of 6 pages"})

    update = next(c for c in calls if c[0] == "execute" and "UPDATE runs" in c[1])
    # (status, stage, partial, reason, stats, run_id)
    assert update[2][0] == "FAILED", "a partial run must not read COMPLETED"
    assert update[2][2] is True, "partial must be recorded as a flag, not a message"
    assert "runtime budget" in update[2][3]


def test_empty_record_set_writes_no_copy():
    from app.repositories.postgres_repo import PostgresRepo
    calls: list = []
    repo = PostgresRepo.__new__(PostgresRepo)
    repo._dsn = "postgresql://unused"
    repo._lock = threading.Lock()
    conn = _FakeConn(calls)
    repo._conn = lambda: conn
    repo.finalize_dataset("11111111-1111-1111-1111-111111111111",
                          {"goal": "g", "fields": []}, [], {})
    assert not [c for c in calls if c[0] == "copy"]


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
    # Offline, stated rather than arranged. This fixture was getting an offline
    # provider by accident: `chdir(tmp_path)` used to hide the repo's .env, so
    # OPENCODE_ENABLED fell back to its default and no live model was ever
    # reached. Config now resolves .env by absolute path - which is the point,
    # because a CWD-relative env_file silently emptied the password and turned
    # every extraction into a 401 - so the intent has to be explicit.
    #
    # setattr, not setenv: `settings` is a module-level singleton built at
    # import, so an env var set afterwards is never read.
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
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
    # The full set, not the six that used to be declared. `main._emit` builds
    # `fields_verified`, `fields_judgment_unavailable`, `records_fully_verified`
    # and their siblings on every terminal event, and pydantic dropped all of
    # them — so this endpoint reported `verified: 0, needs_review: 0` for every
    # run in the database. `tests/test_run_reporting.py` checks the same
    # relationship from the other direction, reading the emitter's source.
    assert set(st["counters"]) == {
        "attempted", "successful", "failed", "records", "verified",
        "needs_review", "fields_verified", "fields_unverified",
        "fields_judgment_unavailable", "fields_rate_limited",
        "records_fully_verified", "records_needing_review",
    }
    # A budget-stopped run must be distinguishable from a clean one. Both keys
    # were computed on every terminal event and unreachable, because RunView had
    # no fields for them.
    assert "partial" in st and "no_yield_reason" in st
    assert st["partial"] is False
