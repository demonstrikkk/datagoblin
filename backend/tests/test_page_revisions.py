"""A `page_id` is an immutable reference to specific text.

`003_evidence.sql` created `UNIQUE (run_id, url)` and said why:

    One stored page per URL per run: re-fetching the same URL updates the
    evidence rather than silently forking it into two contradicting snapshots.

The constraint held and the consequence was worse than the thing it prevented.
`upsert_page` wrote `ON CONFLICT (run_id, url) DO UPDATE` without `id` in the SET
list, so the row kept its primary key while `markdown` was replaced.

Evidence cites `page_id`. Nothing else. So a re-fetch of the same URL inside the
same run left every `start`/`end` already recorded against that id addressing
different text — and the cell stayed `verified`, because nothing re-ran
`locate_quote` and nothing compared the offsets afterwards. The constraint did
not prevent two contradicting rows; it converted them into one row that lies.

The rule these tests pin: **a `page_id` always addresses the same text.**

Run against `LocalRepo`, which is where the suite can reach a real store without
a database. The Postgres statement is asserted by source, since a second
assertion of the same rule against a fake cursor would prove nothing extra.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.local_repo import LocalRepo  # noqa: E402
from app.schemas.evidence import locate_quote  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402


TEXT_V1 = ("Acme Corp was founded in 2011 by Jane Roe. "
           "Jane Roe is the chief executive of Acme Corp.")
TEXT_V2 = ("Acme Corp was founded in 2011 by Jane Roe, who stepped down in 2024. "
           "Priya Shah is the chief executive of Acme Corp.")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = LocalRepo()
    assert r is not None
    return r


def _page(markdown, chash, url="https://lists.example/acme"):
    return {"url": url, "markdown": markdown, "raw_html": f"<html>{markdown}</html>",
            "content_hash": chash, "snapshot_chars": len(markdown),
            "status": "ok", "method": "http",
            "reducer_version": reducer_svc.REDUCER_VERSION}


# --- the invariant ------------------------------------------------------------

def test_a_recorded_offset_still_resolves_after_the_page_changes(repo):
    """The whole reason this change exists, end to end.

    Record a quote against revision 1. Re-fetch the same URL with different
    content. The old offset must still slice the old text -- because it is still
    the id the evidence names.
    """
    pid1 = repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    start, end = locate_quote("Jane Roe is the chief executive", TEXT_V1)
    assert (start, end) != (None, None)
    assert TEXT_V1[start:end] == "Jane Roe is the chief executive"

    pid2 = repo.upsert_page("run1", _page(TEXT_V2, "h2"))
    assert pid2 != pid1, (
        "changed content reused the id, so the recorded offsets above now "
        "address text that was never verified")

    stored = repo.get_page(pid1)
    assert stored["markdown"] == TEXT_V1, (
        "revision 1 was overwritten; the row a verified quote cites no longer "
        "holds the text it was verified against")
    assert stored["markdown"][start:end] == "Jane Roe is the chief executive"


def test_an_identical_refetch_keeps_the_same_id(repo):
    """Idempotence, and it has to hold.

    A BFS re-encounter, a retried rung or a gap-fill pass re-searching a URL the
    run already read all re-store the same bytes. Forking those would inflate
    page counts, waste backfill's candidate budget and double-count sources --
    and would make every quote ambiguous about which row it meant.
    """
    first = repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    again = repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    assert again == first

    third = repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    assert third == first
    assert len(repo.get_pages("run1", 500)) == 1


def test_revisions_increment_and_are_ordered(repo):
    repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    repo.upsert_page("run1", _page(TEXT_V2, "h2"))
    repo.upsert_page("run1", _page(TEXT_V2 + " Updated.", "h3"))

    rows = [p for p in repo.get_pages("run1", 500)]
    assert [p["revision"] for p in rows] == [1, 2, 3]
    assert [p["id"] for p in rows] == list(dict.fromkeys(p["id"] for p in rows)), \
        "every revision must be its own row"


def test_a_caller_supplied_page_id_is_honoured_only_the_first_time(repo):
    """Reusing an id that evidence already cites is the bug, not the feature."""
    first = repo.upsert_page("run1", {**_page(TEXT_V1, "h1"), "page_id": "chosen"})
    assert first == "chosen"

    second = repo.upsert_page("run1", {**_page(TEXT_V2, "h2"), "page_id": "chosen"})
    assert second != "chosen", (
        "the caller's id was reused for different content, so evidence citing it "
        "would silently re-point at new text")


def test_different_runs_are_unaffected(repo):
    """The rule is per (run, url). Two runs each get revision 1."""
    a = repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    b = repo.upsert_page("run2", _page(TEXT_V2, "h2"))
    assert a != b
    assert [p["revision"] for p in repo.get_pages("run1", 500)] == [1]
    assert [p["revision"] for p in repo.get_pages("run2", 500)] == [1]


# --- reducer version ----------------------------------------------------------

def test_a_stored_page_records_which_reducer_produced_its_text(repo):
    repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    rows = repo.get_pages("run1", 500)
    assert rows[0]["reducer_version"] == str(reducer_svc.REDUCER_VERSION)


def test_pages_from_another_reducer_are_findable(repo):
    """The column is only worth having if something can act on it.

    Change `page_evidence_text` and every stored offset reinterprets. That has
    to be answerable per page rather than a suspicion about the whole table.
    """
    repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    assert repo.stale_reducer_pages("run1") == []

    repo.upsert_page("run1", {**_page(TEXT_V2, "h2"),
                              "reducer_version": reducer_svc.REDUCER_VERSION - 1})
    stale = repo.stale_reducer_pages("run1")
    assert len(stale) == 1, (
        "a page stored by a different reducer must be reported, or the column "
        "is decoration")
    assert stale[0]["reducer_version"] != str(reducer_svc.REDUCER_VERSION)


def test_reducer_version_is_an_int(repo):
    """It is compared numerically in code and as a string in SQL.

    Stored as text, so it has to read back as something both can compare.
    """
    assert isinstance(reducer_svc.REDUCER_VERSION, int)
    repo.upsert_page("run1", _page(TEXT_V1, "h1"))
    row = repo.get_pages("run1", 500)[0]
    assert str(row["reducer_version"]) == str(reducer_svc.REDUCER_VERSION)
    assert int(row["reducer_version"]) == reducer_svc.REDUCER_VERSION


# --- the other adapter --------------------------------------------------------

def test_postgres_does_not_update_a_pages_row_in_place():
    """Source guard on the statement that caused the drift.

    `ON CONFLICT ... DO UPDATE` is not banned outright -- it is right for
    `sources`, where a row records that a fetch happened. It is banned in
    `upsert_page`, because for `pages` the row *is* the evidence, and updating it
    in place is what let a quoted offset drift.

    Scans string constants rather than the text, because this test's own
    explanation has to be able to name the mistake it forbids, and a text match
    would then fail on the explanation.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "app" / "repositories"
           / "postgres_repo.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "upsert_page")

    # The docstring has to be able to name the mistake, so it is excluded by
    # identity rather than by pattern -- otherwise this guard fails on its own
    # explanation, which is how guards come to be switched off.
    doc = None
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(
            getattr(fn.body[0], "value", None), ast.Constant):
        doc = fn.body[0].value

    strings = [n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n is not doc]
    sql = [s for s in strings if "INSERT" in s.upper() or "UPDATE" in s.upper()]
    assert sql, "found no SQL to inspect, so this guard proves nothing"

    offenders = [s for s in sql if "DO UPDATE" in s]
    assert not offenders, (
        "upsert_page updates a pages row in place; a re-fetch must append a new "
        "revision so quoted offsets keep addressing the text they were "
        f"verified against:\n{offenders}")
    assert any("revision" in s for s in sql), (
        "the insert must record a revision, or nothing distinguishes the rows")


def test_the_migration_drops_the_constraint_that_made_it_possible():
    """The unique index is the mechanism; removing it is the fix."""
    mig = (Path(__file__).resolve().parents[1] / "migrations"
           / "009_page_revisions.sql").read_text(encoding="utf-8")
    assert "DROP INDEX IF EXISTS pages_run_url_idx" in mig
    assert "ADD COLUMN IF NOT EXISTS revision" in mig
    assert "ADD COLUMN IF NOT EXISTS reducer_version" in mig
    assert "CREATE UNIQUE INDEX" not in mig, (
        "a unique (run, url) index is what forced the in-place update")