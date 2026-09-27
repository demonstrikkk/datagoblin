"""Persistence selection and adapter contract. No database and no network.

The failure this locks in: the adapter used to be inferred from whether
Supabase keys were present, and their absence silently downgraded the app to
JSONL. A fully migrated, empty Postgres sat unused while 379KB of JSONL was
written, and no endpoint, log line or health check reported it. These tests
make the choice explicit and the surface uniform.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.repositories import factory  # noqa: E402
from app.repositories.local_repo import LocalRepo  # noqa: E402
from app.repositories.postgres_repo import PostgresRepo  # noqa: E402

#: Every method the runner, the API and the crawler call on a repository.
REQUIRED = (
    "create_run", "update_run", "upsert_source", "upsert_page", "append_event",
    "finalize_dataset", "get_run", "run_history", "list_datasets", "get_dataset",
    "get_records", "get_sources", "get_pages", "get_page", "save_export",
    "record_charge", "ledger", "note_fingerprint", "seen_fingerprint",
    "save_plan", "get_plan",
)


# --- adapter contract -------------------------------------------------------

@pytest.mark.parametrize("cls", [LocalRepo, PostgresRepo])
def test_adapter_implements_the_full_surface(cls):
    """A missing method is an AttributeError mid-run, not a startup failure."""
    missing = [m for m in REQUIRED if not callable(getattr(cls, m, None))]
    assert not missing, f"{cls.__name__} is missing {missing}"


def test_both_adapters_expose_the_same_method_names():
    """Divergence here is how /sources started returning a different shape
    depending on which adapter was live."""
    local = {m for m in dir(LocalRepo) if not m.startswith("_")}
    pg = {m for m in dir(PostgresRepo) if not m.startswith("_")}
    # LocalRepo predates the evidence store, so it is the adapter that must
    # catch up rather than the other way round.
    assert pg - local <= {"recover_orphan_runs"}, f"PostgresRepo-only: {pg - local}"


def test_local_repo_also_exposes_the_evidence_methods():
    missing = [m for m in ("upsert_page", "get_pages", "get_page", "update_run")
               if not callable(getattr(LocalRepo, m, None))]
    assert not missing, f"LocalRepo missing {missing} - the two adapters must agree"


# --- explicit selection -----------------------------------------------------

def test_persistence_local_is_honoured_even_with_a_dsn(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "PERSISTENCE", "local")
    monkeypatch.setattr(config_mod.settings, "DATABASE_URL", "postgresql://real/db")
    assert isinstance(factory.build_repo(), LocalRepo)
    assert factory.ACTIVE["adapter"] == "local"


def test_persistence_postgres_is_the_default(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "PERSISTENCE", "postgres")
    monkeypatch.setattr(config_mod.settings, "DATABASE_URL", "postgresql://real/db")
    assert isinstance(factory.build_repo(), PostgresRepo)
    assert factory.ACTIVE["adapter"] == "postgres"


def test_missing_dsn_falls_back_to_local_and_says_so(monkeypatch):
    """No DSN means local is the only option, but it must be visible."""
    monkeypatch.setattr(config_mod.settings, "PERSISTENCE", "postgres")
    monkeypatch.setattr(config_mod.settings, "DATABASE_URL", "")
    assert isinstance(factory.build_repo(), LocalRepo)
    assert factory.ACTIVE["adapter"] == "local"
    assert "DATABASE_URL" in factory.ACTIVE["detail"]


def test_unknown_persistence_value_is_rejected(monkeypatch):
    """A typo must fail loudly rather than quietly picking something."""
    monkeypatch.setattr(config_mod.settings, "PERSISTENCE", "postgress")
    with pytest.raises(ValueError):
        factory.build_repo()


def test_health_reports_the_active_adapter():
    """The whole point: which store is live must be observable at runtime."""
    from app import main as main_mod
    assert hasattr(factory, "ACTIVE")
    factory.ACTIVE.update(adapter="local", detail="test")
    import asyncio
    out = asyncio.run(main_mod.health())
    assert out["data"]["persistence"]["adapter"] == "local"


# --- plans survive a restart ------------------------------------------------

def test_plan_round_trips_through_the_local_store(tmp_path):
    """Plans lived only in a process dict, so every plan_id died on restart and
    a recovered run had nothing to resume from."""
    repo = LocalRepo(root=str(tmp_path))
    plan = {"goal": "find acme", "fields": [{"name": "company", "type": "string"}],
            "max_pages": 4}
    repo.save_plan("plan-1", plan)
    got = repo.get_plan("plan-1")
    assert got is not None and got["goal"] == "find acme"
    assert got["max_pages"] == 4
    assert repo.get_plan("no-such-plan") is None


_PLAN = {"goal": "find acme", "entity": "company", "requested_count": 1,
         "max_results": 1,
         "fields": [{"name": "company", "type": "string",
                     "description": "Name", "required": True}],
         "search_queries": ["acme"], "dedupe_keys": ["company"],
         "seed_urls": [], "allowed_sources": [], "max_pages": 1,
         "traversal": {"max_pages_per_domain": 1}, "validation_rules": []}


def test_plan_lookup_is_used_when_memory_is_empty(tmp_path, monkeypatch):
    """`POST /api/runs` with a plan compiled before a restart must still work."""
    from fastapi.testclient import TestClient
    from app import main as main_mod

    repo = LocalRepo(root=str(tmp_path))
    repo.save_plan("plan-persisted", dict(_PLAN))
    # Patch the factory, not REPO: the TestClient runs the startup hook, which
    # calls build_repo() and would otherwise replace the patched repository.
    monkeypatch.setattr(main_mod, "build_repo", lambda: repo)
    main_mod.PLANS.clear()  # the process that compiled it has died

    with TestClient(main_mod.app) as client:
        r = client.post("/api/runs", json={"plan_id": "plan-persisted"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["run_id"]


def test_unknown_plan_id_is_still_rejected(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "build_repo", lambda: LocalRepo(root=str(tmp_path)))
    main_mod.PLANS.clear()
    with TestClient(main_mod.app) as client:
        r = client.post("/api/runs", json={"plan_id": "never-existed"})
    assert r.status_code in (400, 422)
    assert "plan_id" in r.text.lower() or "unknown" in r.text.lower()
