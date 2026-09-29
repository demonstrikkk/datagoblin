"""The backfill endpoints: what they report, and what they refuse."""
import pytest
from fastapi.testclient import TestClient

from app.services import backfill as bf


@pytest.fixture
def client():
    from app import main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


class _Repo:
    def __init__(self, records=None, pages=None, plan=None, dataset=None):
        self.records = records or []
        self.pages = pages if pages is not None else [{
            "id": "p1", "url": "https://x.example/", "markdown": "Acme raised $9M",
            "raw_html": "<html>Acme raised $9M</html>", "title": "t",
            "content_hash": "h", "method": "http", "run_id": "r1",
            "retrieved_at": "2026-09-01T00:00:00Z"}]
        self.plan = plan if plan is not None else {
            "fields": [{"name": "name"}, {"name": "funding"}],
            "dedupe_keys": ["name"]}
        self.dataset = dataset if dataset is not None else {
            "id": "d1", "run_id": "r1", "schema": [{"name": "name"}, {"name": "funding"}]}
        self.written = []

    def get_dataset(self, did):
        return self.dataset if did == "d1" else None

    def get_dataset_schema(self, did):
        # The route uses this to answer "does this dataset exist" without
        # loading its records, so the double has to carry it too.
        return self.dataset.get("schema") if did == "d1" else None

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


def _cell(v, status="unverified"):
    return {"value": v, "verification_status": status,
            "source": {"quote": "q", "url": "u", "page_id": "p1"}}


def _use(monkeypatch, repo):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "REPO", repo)
    monkeypatch.setattr(main_mod, "repo", lambda: repo)


def test_the_proposal_endpoint_reports_cost_and_never_writes(monkeypatch, client):
    repo = _Repo(records=[{"record_id": "1", "fields": {"name": _cell("Acme")}}])
    _use(monkeypatch, repo)
    out = client.get("/api/datasets/d1/backfill").json()["data"]
    assert out["fields"] == ["funding"]
    assert out["backfillable"] is True
    assert out["estimated_extractions"] == 1
    assert out["estimated_judge_calls"] > 0
    assert repo.written == []


def test_the_proposal_404s_for_an_unknown_dataset(monkeypatch, client):
    _use(monkeypatch, _Repo(dataset=None))
    assert client.get("/api/datasets/nope/backfill").status_code == 404


def test_a_plan_without_identity_keys_is_refused_not_attempted(monkeypatch, client):
    repo = _Repo(records=[{"record_id": "1", "fields": {"name": _cell("Acme")}}],
                 plan={"fields": [{"name": "name"}, {"name": "funding"}],
                       "dedupe_keys": []})
    _use(monkeypatch, repo)
    out = client.get("/api/datasets/d1/backfill").json()["data"]
    assert out["backfillable"] is False
    assert "no dedupe_keys" in out["reason"]


def test_the_dry_run_is_the_default_and_writes_nothing(monkeypatch, client):
    """A backfill is a write to a dataset of real extracted data. It must be
    asked for twice: once to see, once to do."""
    repo = _Repo(records=[{"record_id": "1", "fields": {"name": _cell("Acme")}}])
    _use(monkeypatch, repo)
    out = client.post("/api/datasets/d1/backfill", json={}).json()["data"]
    assert out["applied"] is False
    assert repo.written == []


def test_an_apply_that_cannot_match_returns_422_and_writes_nothing(monkeypatch, client):
    repo = _Repo(records=[{"record_id": "1", "fields": {"name": _cell("Acme")}}],
                 plan={"fields": [{"name": "name"}, {"name": "funding"}],
                       "dedupe_keys": []})
    _use(monkeypatch, repo)
    r = client.post("/api/datasets/d1/backfill", json={"apply": True})
    assert r.status_code == 422
    assert "dedupe_keys" in r.json()["error"]["message"]
    assert repo.written == []


def test_a_malformed_page_limit_is_bounded(monkeypatch, client):
    """The limit decides how many stored pages get re-read, so it decides the
    bill. Rejected rather than clamped: silently turning a typo into 1 would let
    a backfill report success while quietly doing a twentieth of the work."""
    _use(monkeypatch, _Repo(records=[{"record_id": "1", "fields": {"name": _cell("Acme")}}]))
    for bad in ("0", "99999", "-4"):
        assert client.get(f"/api/datasets/d1/backfill?limit_pages={bad}").status_code == 422, bad
    assert client.get("/api/datasets/d1/backfill?limit_pages=8").status_code == 200
