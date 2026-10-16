"""Post-MVP hardening: one run shape, honest envelopes, reachable rungs.

Each test pins a defect the status docs recorded but the code still had:

* stored runs returned raw rows (`id`, legacy counters) while live runs
  returned `RunView`, so quality detail depended on which endpoint was asked;
* `/records` and `/sources` conflated "no rows" with "no dataset";
* CSV/MD downloads carried no receipt while JSON did;
* `REDUCING`/`FINALIZING` were published stages that were never emitted;
* `web:jina` and `document:docling` were routes to rungs that could not run;
* empty `dedupe_keys` fell back to every field, so near-duplicates never merged.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.services import deduper as deduper_svc  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REQUIRE_KEYS_AT_STARTUP", "false")
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
    with TestClient(main_mod.app) as c:
        yield c


def test_stored_run_view_unifies_legacy_rows():
    """A raw stored row becomes the same shape as a live run view."""
    row = {"id": "r1", "status": "FAILED", "current_stage": "FAILED",
           "progress": 100, "partial": True, "error": "budget spent",
           "stats": {"counts": {"attempted": 5, "successful": 4, "failed": 1,
                                "records": 2, "fields_verified": 3}}}
    view = main_mod._stored_run_view(row, "d1")
    assert view["run_id"] == "r1"
    assert view["dataset_id"] == "d1"
    assert view["partial"] is True
    assert view["no_yield_reason"] in ("", "budget spent")
    for key in ("attempted", "successful", "failed", "records",
                "fields_verified", "fields_conflicting"):
        assert key in view["counters"], key


def test_unknown_dataset_is_404_but_empty_is_200(client):
    assert client.get("/api/datasets/00000000-0000-0000-0000-000000000000/records").status_code == 404
    assert client.get("/api/datasets/00000000-0000-0000-0000-000000000000/sources").status_code == 404

    repo = main_mod.repo()
    did = repo.finalize_dataset(
        "r1", {"goal": "t", "fields": [{"name": "company_name"}]},
        [{"fields": {"company_name": {"value": "Acme",
                                      "verification_status": "verified",
                                      "source": {"url": "https://x.example/",
                                                 "quote": "Acme",
                                                 "retrieved_at": "2026-01-01T00:00:00Z"}}}}],
        {"attempted": 1, "successful": 1, "failed": 0})
    assert client.get(f"/api/datasets/{did}/records").status_code == 200
    assert client.get(f"/api/datasets/{did}/sources").status_code == 200
    hist = client.get("/api/history").json()["data"]
    assert any(h.get("run_id") == "r1" for h in hist)

    exp = client.post(f"/api/datasets/{did}/export", json={"format": "csv"})
    assert exp.status_code == 200
    for header in ("x-datagoblin-export-id", "x-datagoblin-credits-used",
                   "x-datagoblin-correlation-id"):
        assert header in {k.lower() for k in exp.headers}, header


def test_gated_rungs_are_ordered_only_when_enabled(monkeypatch):
    from app.core.config import settings as _settings
    monkeypatch.setattr(_settings, "FEATURE_JINA", False)
    monkeypatch.setattr(_settings, "FEATURE_DOCLING", False)
    assert "jina" not in crawler_svc._methods_for("web:http", "https://x.example/")
    assert crawler_svc._methods_for("document:docling",
                                    "https://x.example/a.pdf") == ("http", "crawl4ai")

    monkeypatch.setattr(_settings, "FEATURE_JINA", True)
    monkeypatch.setattr(_settings, "FEATURE_DOCLING", True)
    assert crawler_svc._methods_for("web:http", "https://x.example/")[-1] == "jina"
    assert crawler_svc._methods_for("document:docling",
                                    "https://x.example/a.pdf")[0] == "docling"


def test_disabled_docling_refuses_with_a_name():
    import asyncio
    from app.core.config import settings as _settings
    assert _settings.FEATURE_DOCLING is False
    with pytest.raises(AppError) as exc:
        asyncio.run(main_mod._fetch_method("https://x.example/a.pdf", "docling"))
    assert exc.value.code == "E_PROVIDER_FATAL"


def test_empty_dedupe_keys_use_identity_fields():
    assert deduper_svc._fallback_keys(["company_name", "funding", "notes"]) == ["company_name"]
    rows = [
        {"fields": {"company_name": {"value": "Acme Inc"},
                    "notes": {"value": "first sighting"}}},
        {"fields": {"company_name": {"value": "acme  inc"},
                    "notes": {"value": "second sighting"}}},
    ]
    canonical, merged = deduper_svc.dedupe_records(rows, [])
    assert merged == 1 and len(canonical) == 1

    rivals = [
        {"fields": {"company_name": {"value": "Acme Inc"}}},
        {"fields": {"company_name": {"value": "Acme LLC"}}},
    ]
    canonical, merged = deduper_svc.dedupe_records(rivals, [])
    assert merged == 0 and len(canonical) == 2


def test_runner_emits_reducing_and_finalizing():
    import asyncio
    from app.services import runner as runner_svc

    events: list[dict] = []

    async def _emit(ev):
        events.append(ev)

    plan = {"goal": "t", "entity": "company", "requested_count": 1,
            "max_results": 1, "search_queries": ["q"],
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "seed_urls": [], "allowed_sources": [], "max_pages": 1,
            "traversal": {"max_pages_per_domain": 1}, "validation_rules": [],
            "dedupe_keys": ["company_name"]}
    page = {"url": "https://x.example/", "markdown": "Acme " * 200, "html": ""}

    async def _fetch(url, method):
        return dict(page)

    async def _llm(prompt, schema):
        return {"data": {"records": [], "coverage": "none"}, "provider": "t"}

    async def _store(rid, pl, rows, counts):
        return "d1"

    async def _noop(*a, **k):
        return None

    ctx = {"search": lambda *a, **k: asyncio.sleep(0, result=[]),
           "fetch": _fetch, "llm": _llm, "store": _store,
           "persist_source": _noop, "persist_page": _noop,
           "reuse": _noop, "cancelled": lambda: False,
           "llm_preflight": lambda: asyncio.sleep(0, result=None)}
    asyncio.run(runner_svc.execute_run("r1", plan, ctx, _emit))
    started = [e["stage"] for e in events if e.get("type") == "stage.started"]
    assert "REDUCING" in started
    assert "FINALIZING" in started


def test_cross_dataset_detection_names_but_never_merges():
    """The audit question — "does this entity exist elsewhere?" — answered
    without touching a single row."""
    own = [{"fields": {"company_name": {"value": "Acme Inc"}}}]
    others = [("d2", [{"fields": {"company_name": {"value": "Acme Inc"},
                                   "funding": {"value": "$5M"}}}]),
              ("d3", [{"fields": {"company_name": {"value": "Globex"}}}])]
    out = deduper_svc.find_cross_dataset_duplicates(own, ["company_name"], others)
    assert out["matches"] == 1
    assert out["matched_datasets"] == ["d2"]
    assert out["examples"][0]["dataset_id"] == "d2"
    assert "nothing was merged" in out["note"]


def test_cross_dataset_detection_refuses_without_keys():
    """Same refusal as backfill/gapfill/refresh: no declared identity, no
    matching. All-fields fallback would answer a different question."""
    with pytest.raises(deduper_svc.CrossDatasetRefused):
        deduper_svc.find_cross_dataset_duplicates(
            [{"fields": {"a": {"value": "x"}}}], [],
            [("d2", [{"fields": {"a": {"value": "x"}}}])])


def test_duplicates_endpoint_detects_without_writing(client):
    def _cell(value, url):
        return {"value": value, "verification_status": "verified",
                "source": {"url": url, "quote": value,
                           "retrieved_at": "2026-01-01T00:00:00Z"}}

    repo = main_mod.repo()
    keyed = {"goal": "t", "fields": [{"name": "company_name"}],
             "dedupe_keys": ["company_name"]}
    repo.create_run("r1", "w1", keyed)
    repo.save_plan("w1", keyed)
    d1 = repo.finalize_dataset(
        "r1", keyed, [{"fields": {"company_name": _cell("Acme Inc", "https://x.example/")}}],
        {"attempted": 1, "successful": 1, "failed": 0})
    repo.create_run("r2", "w2", keyed)
    repo.save_plan("w2", keyed)
    repo.finalize_dataset(
        "r2", keyed, [{"fields": {"company_name": _cell("Acme Inc", "https://y.example/")}}],
        {"attempted": 1, "successful": 1, "failed": 0})

    r = client.get(f"/api/datasets/{d1}/duplicates")
    assert r.status_code == 200, r.text[:300]
    body = r.json()["data"]
    assert body["matches"] == 1
    assert len(body["matched_datasets"]) == 1
    # Nothing was merged: both datasets still hold their own row.
    assert (client.get(f"/api/datasets/{d1}/records").json()["data"]["total"] == 1)

    # No declared keys: refuse rather than match on every field.
    keyless = {"goal": "t3", "fields": [{"name": "company_name"}]}
    repo.create_run("r3", "w3", keyless)
    repo.save_plan("w3", keyless)
    d3 = repo.finalize_dataset(
        "r3", keyless, [{"fields": {"company_name": _cell("Acme Inc", "https://z.example/")}}],
        {"attempted": 1, "successful": 1, "failed": 0})
    r = client.get(f"/api/datasets/{d3}/duplicates")
    assert r.status_code == 422
    assert "dedupe_keys" in r.json()["error"]["message"]