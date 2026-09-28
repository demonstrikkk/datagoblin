"""Phase 1: stored-evidence endpoints, and the two defects found while adding them."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app import main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


class _FakeRepo:
    """Enough of the repository surface for the evidence routes."""

    def __init__(self, run_ids=(), pages=None):
        self.run_ids = set(run_ids)
        self.pages = pages or {}

    def get_run(self, run_id):
        return {"id": run_id} if run_id in self.run_ids else None

    def get_pages(self, run_id, limit=200):
        return [{"id": pid, "run_id": run_id, "url": p.get("url", "")}
                for pid, p in self.pages.items()][:limit]

    def get_page(self, page_id):
        return self.pages.get(page_id)


def _with_repo(monkeypatch, repo):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "REPO", repo)
    monkeypatch.setattr(main_mod, "repo", lambda: repo)


RUN_ID = "11111111-1111-1111-1111-111111111111"
PAGE_ID = "22222222-2222-2222-2222-222222222222"


def test_page_detail_returns_markdown_and_drops_raw_html(monkeypatch, client):
    """`markdown` is what a quote's offsets index into; raw_html is the snapshot.

    Dropping raw_html is deliberate — it is routinely megabytes and no quoting
    path reads it. Returning it would make a single proof click fetch the whole
    document.
    """
    _with_repo(monkeypatch, _FakeRepo(pages={
        PAGE_ID: {"id": PAGE_ID, "url": "https://x.example/", "run_id": RUN_ID,
                  "markdown": "alpha beta gamma", "raw_html": "<html>" + "z" * 5000 + "</html>"},
    }))
    out = client.get(f"/api/pages/{PAGE_ID}").json()
    assert out["error"] is None
    assert out["data"]["markdown"] == "alpha beta gamma"
    assert "raw_html" not in out["data"]


def test_page_detail_404_for_an_id_that_does_not_exist(monkeypatch, client):
    _with_repo(monkeypatch, _FakeRepo())
    r = client.get(f"/api/pages/{PAGE_ID}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_NOT_FOUND"


def test_run_pages_lists_evidence_for_a_known_run(monkeypatch, client):
    _with_repo(monkeypatch, _FakeRepo(run_ids=[RUN_ID], pages={
        PAGE_ID: {"id": PAGE_ID, "url": "https://x.example/"},
    }))
    out = client.get(f"/api/runs/{RUN_ID}/pages").json()
    assert out["error"] is None
    assert out["data"]["run_id"] == RUN_ID
    assert [p["id"] for p in out["data"]["pages"]] == [PAGE_ID]


def test_run_pages_404_for_an_unknown_run(monkeypatch, client):
    _with_repo(monkeypatch, _FakeRepo())
    assert client.get(f"/api/runs/{RUN_ID}/pages").status_code == 404


def test_malformed_id_is_rejected_as_invalid_input(monkeypatch, client):
    """A garbage id used to come back 503 E_DEPENDENCY.

    Both repositories key on uuid, so a value that is not one reaches Postgres
    and fails the cast; `dependency()` then reports an infrastructure failure.
    The caller sent nonsense, which is a validation error — saying the database
    is down is a lie that sends someone to the wrong dashboard.
    """
    _with_repo(monkeypatch, _FakeRepo())
    for path in ("/api/pages/not-a-uuid", f"/api/runs/not-a-uuid/pages"):
        r = client.get(path)
        assert r.status_code == 422, f"{path} -> {r.status_code}"
        assert r.json()["error"]["code"] == "E_VALIDATION", r.text


def test_partial_runs_are_evictable_like_other_terminal_runs():
    """PARTIAL was missing from _TERMINAL, so it could never be retired.

    Eviction only ever picks candidates from that tuple, and the loop runs only
    while candidates remain — so once RUNS filled with budget-stopped runs the
    loop found nothing to pop and the registry grew without bound. COMPLETED
    runs were retired normally, which made it look like eviction was working.
    """
    from app import main as main_mod

    saved_runs, saved_tasks = dict(main_mod.RUNS), dict(main_mod.TASKS)
    try:
        main_mod.RUNS.clear()
        main_mod.TASKS.clear()
        for i in range(main_mod.MAX_KEPT_RUNS + 5):
            main_mod.RUNS[f"p{i:05d}"] = {"status": "PARTIAL"}
        main_mod._evict_registries()
        assert len(main_mod.RUNS) == main_mod.MAX_KEPT_RUNS, (
            f"PARTIAL runs were not evicted: {len(main_mod.RUNS)} remain"
        )
        # And the still-running states must not be touched by eviction.
        assert "PARTIAL" in main_mod._TERMINAL
        assert "DISCOVERING" not in main_mod._TERMINAL
    finally:
        main_mod.RUNS.clear()
        main_mod.RUNS.update(saved_runs)
        main_mod.TASKS.clear()
        main_mod.TASKS.update(saved_tasks)
