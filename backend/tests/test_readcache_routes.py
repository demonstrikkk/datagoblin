"""The cached read routes must serve the cache and must not serve stale data.

The unit tests in `test_readcache.py` prove the cache behaves. These prove the
routes actually use it, and — the part that would be a real bug — that a write
makes the change visible immediately instead of after the TTL.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.services import readcache  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Mirrors `test_api_p0.py`: LocalRepo, offline, no keys.

    Stated rather than assumed — see the note there about `chdir` no longer
    hiding `.env` once config resolves it absolutely, and about `settings`
    being a singleton built at import.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
    with TestClient(main_mod.app) as c:
        yield c


def _dataset(client, name, fields):
    """Create a finalized dataset with rows, the way a run does.

    There is no `POST /api/datasets` — a dataset comes into existence when a run
    finalizes it, so that is the path exercised here rather than a test-only
    shortcut that would not prove the real thing.
    """
    plan = {"goal": "g", "entity": "e",
            "fields": [{"name": f, "dtype": "text"} for f in fields],
            "search_queries": ["q"], "dedupe_keys": [],
            "traversal": {"max_pages": 5, "max_depth": 1,
                          "max_pages_per_domain": 2}}
    rows = [{"fields": {f: f"v-{i}" for f in fields}, "url": f"https://s.example/{i}",
             "title": f"r{i}", "verdict": "verified"} for i in range(3)]
    stats = {"attempted": 3, "successful": 3, "failed": 0, "skipped": 0,
             "records": 3, "verified": 3, "needs_review": 0}
    return main_mod.REPO.finalize_dataset("run-cache-test", plan, rows, stats)


def _first_record_id(client, did):
    recs = client.get(f"/api/datasets/{did}/records").json()["data"]["records"]
    return recs[0]["record_id"]


@pytest.fixture(autouse=True)
def _clear():
    readcache.invalidate()
    yield
    readcache.invalidate()


def test_repeat_gaps_read_is_served_from_cache(client):
    did = _dataset(client, "cached", ["a"])
    first = client.get(f"/api/datasets/{did}/gaps").json()
    second = client.get(f"/api/datasets/{did}/gaps").json()
    assert first["data"] == second["data"], "cached read changed the answer"
    assert second["meta"].get("cached") is True


def test_a_write_makes_the_change_visible_before_the_ttl_expires(client):
    """The failure this guards against: cached for 300s, so an edit looks lost."""
    did = _dataset(client, "fresh", ["note"])
    before = client.get(f"/api/datasets/{did}/coverage").json()

    client.post(f"/api/datasets/{did}/columns",
                json={"name": "note2", "dtype": "text"})
    after = client.get(f"/api/datasets/{did}/coverage").json()

    assert after["meta"].get("cached") is False, \
        "the schema write did not invalidate; the new column may be hidden"
    assert len(after["data"]["fields"]) >= len(before["data"]["fields"])


def test_profile_caches_per_limit(client):
    """Different limits are different answers and must not share a key."""
    did = _dataset(client, "profiled", ["a"])
    client.get(f"/api/datasets/{did}/profile?limit=10")
    a = client.get(f"/api/datasets/{did}/profile?limit=10").json()
    b = client.get(f"/api/datasets/{did}/profile?limit=5000").json()

    assert a["meta"].get("cached") is True
    assert b["meta"].get("cached") is False, \
        "profile keyed on dataset only; a smaller limit served a larger answer"


def test_resolving_a_conflict_invalidates_the_derived_reads(client):
    """Same class of bug as the column write: a cell write must invalidate.

    Found by auditing every mutating route for an invalidation rather than by a
    failing test, so it is pinned here deliberately — the resolved conflict used
    to keep appearing open in the tab it was resolved from.

    Cells are provenance-bearing here because that is what `resolve_conflict`
    accepts: it will not write a value nothing extracted, so a plain-value
    record cannot exercise this path at all.
    """
    did = _dataset(client, "conflicted", ["amount"])
    main_mod.REPO.update_record_cell(did, _first_record_id(client, did),
                                     "amount",
                                     {"value": "1", "verification_status": "conflicting",
                                      "source": {"quote": "q", "url": "https://s.example/0",
                                                 "page_id": "p0"}})
    client.get(f"/api/datasets/{did}/coverage")  # warm

    r = client.post(f"/api/datasets/{did}/conflicts/resolve", json={
        "record_id": _first_record_id(client, did),
        "field": "amount", "choice": "keep",
    })
    assert r.status_code == 200, r.text

    after = client.get(f"/api/datasets/{did}/coverage").json()
    assert after["meta"].get("cached") is False, \
        "a cell write did not invalidate; the old value is still being served"


def test_an_unknown_dataset_is_still_404_not_an_empty_cached_answer(client):
    """Existence is checked before the cache, so this stays a 404."""
    for _ in range(2):
        r = client.get("/api/datasets/00000000-0000-0000-0000-000000000000/gaps")
        assert r.status_code == 404