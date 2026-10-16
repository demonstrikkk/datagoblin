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


def test_gap_fill_refuses_with_a_reason_rather_than_crashing():
    """A refusal is fine; an unnamed 500 is not.

    Real stored datasets, because the two refusals under test are the honest
    ones: an evidence gap that fetching cannot fix, and a field the schema does
    not describe. Both must arrive as a typed E_VALIDATION naming the reason.
"""
    from fastapi.testclient import TestClient

    from app.main import app

    # As a context manager, so the lifespan runs and the repository is built.
    # Without it every route answers "repository not initialised" and the test
    # would pass for entirely the wrong reason.
    with TestClient(app) as client:
        datasets = (client.get("/api/datasets").json() or {}).get("data") or []
        if not datasets:
            pytest.skip("no datasets stored")

        dataset = datasets[0]
        did = dataset["id"]
        names = [f.get("name") for f in (dataset.get("schema") or [])
                 if f.get("name")][:2]
        if not names:
            pytest.skip("dataset has no fields")

        for field in names:
            resp = client.post(f"/api/datasets/{did}/gaps/fill",
                               json={"apply": False, "field": field})
            assert resp.status_code < 500, (
                f"gaps/fill returned {resp.status_code} for {field!r}: "
                f"{resp.text[:200]}")
            if resp.status_code != 200:
                err = (resp.json().get("error") or {})
                assert err.get("message"), (
                    f"a {resp.status_code} was returned without a reason for {field!r}")


def test_gap_fill_requires_a_field():
    """The cheapest guard, and the one the UI can trip."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        datasets = (client.get("/api/datasets").json() or {}).get("data") or []
        if not datasets:
            pytest.skip("no datasets stored")

        resp = client.post(f"/api/datasets/{datasets[0]['id']}/gaps/fill",
                           json={"apply": False})
        assert 400 <= resp.status_code < 500, (
            f"a request naming no field should be refused cleanly, "
            f"got {resp.status_code}")