"""The cross-run source aggregate.

`GET /api/sources` exists because the source browser had no read to call: pages
are scoped to one run and sources to one dataset, so assembling "every host this
workspace has ever fetched" meant one request per run from the browser.

These tests pin the two things that could go quietly wrong: the aggregation
folding, and the endpoint not 500-ing on a repository that lacks the method.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.repositories.local_repo import LocalRepo  # noqa: E402


# --------------------------------------------------------------------- fold


def _page(run_id: str, url: str, chars: int = 100, at: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "id": f"p-{run_id}-{url}",
        "run_id": run_id,
        "url": url,
        "snapshot_chars": chars,
        "retrieved_at": at,
    }


@pytest.fixture()
def repo(tmp_path):
    return LocalRepo(tmp_path / "dg.json")


def test_no_pages_yields_no_hosts(repo: LocalRepo) -> None:
    assert repo.list_sources() == []


def test_pages_group_by_host_without_www(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "https://www.nseindia.com/a"))
    repo.upsert_page("r1", _page("r1", "https://nseindia.com/b"))
    hosts = {h["host"]: h for h in repo.list_sources()}
    assert "nseindia.com" in hosts
    assert "www.nseindia.com" not in hosts
    assert hosts["nseindia.com"]["pages"] == 2


def test_characters_sum_per_host(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "https://a.test/1", chars=100))
    repo.upsert_page("r1", _page("r1", "https://a.test/2", chars=250))
    host = next(h for h in repo.list_sources() if h["host"] == "a.test")
    assert host["chars"] == 350


def test_runs_counted_distinctly_not_by_page(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "https://a.test/1"))
    repo.upsert_page("r1", _page("r1", "https://a.test/2"))
    repo.upsert_page("r2", _page("r2", "https://a.test/3"))
    host = next(h for h in repo.list_sources() if h["host"] == "a.test")
    assert host["pages"] == 3
    assert host["runs"] == 2


def test_ordered_by_pages_then_characters(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "https://small.test/1"))
    repo.upsert_page("r1", _page("r1", "https://big.test/1"))
    repo.upsert_page("r1", _page("r1", "https://big.test/2"))
    repo.upsert_page("r1", _page("r1", "https://big.test/3"))
    assert [h["host"] for h in repo.list_sources()] == ["big.test", "small.test"]


def test_limit_is_clamped_and_applied(repo: LocalRepo) -> None:
    for i in range(5):
        repo.upsert_page("r1", _page("r1", f"https://h{i}.test/1"))
    assert len(repo.list_sources(limit=2)) == 2
    # A nonsense limit must not raise or return nothing.
    assert repo.list_sources(limit=0)
    assert repo.list_sources(limit=10**9)


def test_last_used_is_the_newest_retrieval(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "https://a.test/1", at="2026-01-01T00:00:00Z"))
    repo.upsert_page("r1", _page("r1", "https://a.test/2", at="2026-03-09T12:00:00Z"))
    repo.upsert_page("r1", _page("r1", "https://a.test/3", at="2026-02-01T00:00:00Z"))
    host = next(h for h in repo.list_sources() if h["host"] == "a.test")
    assert str(host["last_used"]).startswith("2026-03-09")


def test_malformed_url_does_not_crash_the_fold(repo: LocalRepo) -> None:
    repo.upsert_page("r1", _page("r1", "not-a-url"))
    repo.upsert_page("r1", _page("r1", "https://good.test/1"))
    hosts = {h["host"] for h in repo.list_sources()}
    assert "good.test" in hosts
    assert "" in hosts


# ----------------------------------------------------------------- endpoint


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient on the real app, backed by a throwaway local store.

    Same shape as test_api_p0: `chdir` so the local store lands in tmp_path, and
    `settings` patched with `setattr` because it is a singleton built at import,
    so a later env var would never be read.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.core.config import settings as _settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
    with TestClient(main_mod.app) as c:
        yield c, main_mod


def test_endpoint_returns_envelope(client) -> None:
    tc, _ = client
    r = tc.get("/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None
    assert body["data"]["hosts"] == []
    assert body["data"]["unsupported"] is False


def test_endpoint_lists_stored_hosts(client) -> None:
    tc, main_mod = client
    main_mod.REPO.upsert_page("r1", _page("r1", "https://nseindia.com/a", chars=40))
    main_mod.REPO.upsert_page("r1", _page("r1", "https://nseindia.com/b", chars=60))
    body = tc.get("/api/sources").json()["data"]
    assert body["unsupported"] is False
    host = next(h for h in body["hosts"] if h["host"] == "nseindia.com")
    assert host["pages"] == 2
    assert host["chars"] == 100


def test_endpoint_degrades_when_repo_lacks_the_aggregate(client) -> None:
    """A repository without `list_sources` must not 500.

    `repo()` is a bare object with no protocol, so a missing method would
    otherwise surface as an AttributeError and read as a broken backend rather
    than as "this view is not available here".
    """
    tc, main_mod = client

    class Bare:
        def get_run(self, *_a, **_k):
            return None

    main_mod.REPO = Bare()
    r = tc.get("/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["hosts"] == []
    assert body["data"]["unsupported"] is True


def test_endpoint_is_in_the_openapi_contract() -> None:
    import yaml

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "contracts", "api.openapi.yaml"), encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    assert "/api/sources" in spec["paths"]
