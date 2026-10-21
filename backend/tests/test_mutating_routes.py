"""Every mutating route must answer with a typed error, never a 500.

`POST /api/datasets/{id}/gaps/fill` answered 500 for every dataset, always. The
cause was an operator-precedence slip on one line:

    str((await to_thread(r.get_dataset_row, did)) or {}).get("run_id", "")

Python binds `.get` to the `str(...)` result, so the handler raised
`AttributeError: 'str' object has no attribute 'get'` before it did any work —
which surfaced in the Gap Queue as a bare "Internal Server Error" naming no field
and no dataset, with only a stack trace behind it.

Two things are pinned. First the route itself, because a 500 that happens 100%
of the time is exactly the kind that goes unnoticed for a long time. Second the
*shape* of the bug — `str(...)` wrapped around a value that is then
dereferenced. `backfill.py` carries the correct form of the same expression a few
lines away, which is what identifies this as a typo rather than a convention.
"""
from __future__ import annotations

import re
import pathlib

import pytest

MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_no_str_wrapper_hides_a_dereference():
    """`str(x).method(...)` — the method lands on the string, never on x.

    Checked across the whole file rather than at the one known line, because the
    mistake is invisible in review and a second copy would fail identically.
    """
    src = MAIN.read_text(encoding="utf-8")
    # `str(` ... `)` immediately followed by `.name(` is the trap.
    # Comments are skipped: this file documents the bug by quoting it, and a
    # guard that fires on its own explanation is a guard nobody reads.
    offenders = [
        f"main.py:{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if not line.lstrip().startswith("#")
        and re.search(r"str\([^()]*\)\.\w+\(", line)
        and not re.search(
            r"str\([^()]*\)\.(lower|upper|strip|replace|split|join|startswith"
            r"|endswith|format|removeprefix|removesuffix)\(", line)
    ]
    assert not offenders, (
        "a `str(...)` wraps a value that is then called on, so the method binds "
        "to the string and the original is never touched:\n  "
        + "\n  ".join(offenders))


def test_the_gap_fill_route_resolves_a_run_id():
    """`gaps/fill` needs the owning run before it can store a page it fetched."""
    src = MAIN.read_text(encoding="utf-8")
    assert "r.get_dataset_row" in src, "the dataset row lookup is gone"
    assert 'row.get("run_id")' in src, (
        "the run id must be read from the row and only then stringified — "
        "Postgres returns a UUID where LocalRepo returns a str, so both "
        "repositories must work through the same line")


def _client(tmp_path, monkeypatch):
    """A TestClient bound to a throwaway store.

    `TestClient(app)` runs the lifespan, and the lifespan builds the repository
    from the process CWD. Without `chdir` that is the developer's real
    `.datagoblin-local/` -- so these two tests were reading, and posting at, the
    actual datasets, and `pytest.skip`-ing themselves out of existence when that
    store happened to be empty. Coverage that depends on what you last ran is
    not coverage.
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.core.config import settings as _settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_settings, "OPENCODE_ENABLED", False)
    return main_mod, TestClient(main_mod.app)


def _dataset(main_mod, rows=None):
    """A real dataset in the temp store, with the plan schema gapfill reads."""
    plan = {"entity": "Acme", "goal": "find acme corp",
            "fields": [{"name": "company_name", "type": "string",
                        "required": False}]}
    return main_mod.REPO.finalize_dataset(
        "run-hermetic", plan, list(rows or []),
        {"attempted": 0, "successful": 0, "failed": 0, "skipped": 0,
         "records": len(rows or []), "verified": 0, "needs_review": 0})


def test_gap_fill_refuses_with_a_reason_rather_than_crashing(tmp_path, monkeypatch):
    """A refusal is fine; an unnamed 500 is not.

    Hermetic: an empty dataset, so `gaps/fill` refuses with a named reason
    every time. This used to read the first real dataset on the machine and skip
    if there was none, which is how a route that answered 500 for every dataset,
    always, kept looking covered.
    """
    main_mod, app = _client(tmp_path, monkeypatch)

    with app as client:
        did = _dataset(main_mod, rows=[])
        resp = client.post(f"/api/datasets/{did}/gaps/fill",
                           json={"apply": False, "field": "company_name"})
        assert resp.status_code < 500, (
            f"gaps/fill returned {resp.status_code}: {resp.text[:200]}")
        assert resp.status_code >= 400, (
            "an empty dataset has nothing to fill and must be refused")
        err = (resp.json().get("error") or {})
        assert err.get("code") == "E_VALIDATION", err
        assert "nothing to attempt" in (err.get("message") or ""), (
            "a refusal has to say what to do instead, not merely that it "
            f"declined: {err}")


def test_gap_fill_requires_a_field(tmp_path, monkeypatch):
    """The cheapest guard, and the one the UI can trip."""
    main_mod, app = _client(tmp_path, monkeypatch)

    with app as client:
        did = _dataset(main_mod, rows=[])
        resp = client.post(f"/api/datasets/{did}/gaps/fill", json={"apply": False})
        assert 400 <= resp.status_code < 500, (
            f"a request naming no field should be refused cleanly, "
            f"got {resp.status_code}")
        err = (resp.json().get("error") or {})
        assert err.get("code") == "E_VALIDATION", err
        assert "field is required" in (err.get("message") or ""), err


def test_gap_fill_on_an_unknown_dataset_is_404(tmp_path, monkeypatch):
    """Refusals are for real datasets. A missing one is not found."""
    main_mod, app = _client(tmp_path, monkeypatch)

    with app as client:
        resp = client.post("/api/datasets/does-not-exist/gaps/fill",
                           json={"apply": False, "field": "company_name"})
        assert resp.status_code == 404, resp.text


def test_an_attempt_that_wrote_nothing_is_still_recorded(tmp_path, monkeypatch):
    """The gap must advance even when the phase found nothing.

    The route used to upsert the gap only when cells were written, so a
    fruitless phase left the field at phase 0 with `attempts` 0 forever and the
    same search could be run without limit. Recording the attempt is the whole
    reason the gap table exists.
    """
    from app.services import gaps as gaps_svc

    main_mod, app = _client(tmp_path, monkeypatch)
    with app as client:
        did = _dataset(main_mod)
        main_mod.REPO.upsert_gap(did, "company_name", {
            "field": "company_name", "category": "depth_gap",
            "state": "open", "phase": 0, "attempts": 0})

        async def _fake_run(store, dataset_id, field, *, phase, llm, search_fn,
                                fetch_fn, persist_page=None, search_budget=12,
                                apply=False, phrases=None):
            gap = store.get_gap(dataset_id, field)
            after = gaps_svc.record_attempt(
                gap, phase=phase, stats={"cells_written": 0},
                error="phase 1 found nothing; phase 2 not attempted")
            return {"report": {"written": 0, "searches_run": 0, "applied": apply},
                    "gap": after}

        monkeypatch.setattr(main_mod.gapfill_svc, "run", _fake_run)
        resp = client.post(f"/api/datasets/{did}/gaps/fill",
                           json={"field": "company_name", "phase": 1, "apply": True})
        assert resp.status_code == 200, resp.text

        stored = main_mod.REPO.get_gap(did, "company_name")
        assert stored["attempts"] == 1, stored
        assert stored["phase"] == gaps_svc.PHASE_STORED, stored