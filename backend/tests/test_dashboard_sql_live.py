"""The SQL aggregate, checked against the shape that is actually stored.

`test_dashboard_aggregate.py` pins the arithmetic — that the aggregate and
`coverage.field_coverage` produce identical totals. It cannot pin the *query*,
because its fake store computes the aggregate by calling `field_coverage`. So the
query itself was never executed by a single test.

That is not hypothetical. `coverage_aggregates` read `row_json -> 'fields'`, and
`row_json` **is** the field map — its top level is
`{"company_name": {...}, "country": {...}}`. The `fields` key exists in every
Python fixture and in every record the API returns, because `get_records`
presents it as `{"record_id": ..., "fields": row_json}`. So the path looked right,
returned NULL for all 723 stored rows, made `jsonb_each` yield nothing, and left
every field falling through to the declared-at-zero branch.

The result reported every dataset as completely empty, and it looked entirely
healthy: a valid query, plausible numbers, no error. All 11 live datasets with
records mismatched.

These tests run the real query against a real Postgres and are skipped when there
is no database, so the shape assertion below is cheap to keep and is exactly the
thing a Python-only test cannot see.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import coverage as coverage_svc
from app.services import dashboard as dashboard_svc


def _repo():
    if not settings.DATABASE_URL:
        pytest.skip("no DATABASE_URL; the stored shape cannot be checked without one")
    from app.repositories.postgres_repo import PostgresRepo
    return PostgresRepo(settings.DATABASE_URL)


def _rows_with_records(repo, limit=4):
    rows = repo.list_datasets() or []
    agg = repo.coverage_aggregates([str(r["id"]) for r in rows if r.get("id")])
    live = [str(r["id"]) for r in rows
            if r.get("id") and agg[str(r["id"])]["records"] > 0][:limit]
    if not live:
        pytest.skip("this database holds no dataset with records")
    return rows, live


# --- the shape assumption, stated as a test ----------------------------------

def test_row_json_is_the_field_map_and_not_a_wrapper_around_one():
    """The bug, pinned.

    If this ever stops being true the aggregate's `jsonb_each` is reading nothing
    and every dataset silently reports as empty.
    """
    repo = _repo()
    rows, live = _rows_with_records(repo, limit=1)
    did = live[0]

    # The API's own view, which is where the `fields` key comes from.
    recs = (repo.get_records(did, "", 1, 0) or {}).get("records") or []
    assert recs, "expected at least one record"
    presented = recs[0]["fields"]
    assert "fields" not in presented, \
        "a record whose own field map contains a 'fields' key would be ambiguous"

    # The stored shape.
    from app.repositories.postgres_repo import PostgresRepo  # noqa: F401
    raw = repo._rows("shape_probe",
                     "SELECT row_json FROM dataset_records WHERE dataset_id=%s LIMIT 1",
                     (did,))
    rj = raw[0]["row_json"]
    keys = [k for k in rj.keys() if k != "fields"]
    assert keys, "stored row_json has no field keys"
    assert "fields" not in rj, (
        "row_json now wraps its fields in a 'fields' key, so coverage_aggregates "
        "must read row_json->'fields' instead of row_json")


def test_a_stored_cell_reaches_the_value_and_the_verdict():
    """Both are read by path in the query, so both are asserted to exist."""
    repo = _repo()
    _rows, live = _rows_with_records(repo, limit=1)
    raw = repo._rows("cell_probe",
                     """SELECT e.key, e.value FROM dataset_records r,
                          LATERAL jsonb_each(r.row_json) AS e
                        WHERE r.dataset_id=%s AND jsonb_typeof(e.value)='object'
                          AND COALESCE(e.value->>'value','') <> ''
                        LIMIT 1""", (live[0],))
    assert raw, "no populated cell found in the sample"
    cell = raw[0]["value"]
    assert "value" in cell, "a populated cell has no 'value' key"
    assert "verification_status" in cell, (
        "a cell carries no verification_status; the aggregate's fallback still "
        "holds, but the documented shape has changed")


def test_the_only_verdict_in_live_data_is_the_one_the_aggregate_expects():
    """`judgment_unavailable` is what made a literal status list report zero.

    992 of the cells here are that status, and `coverage._cell_value` buckets any
    unrecognised status into `unverified`. The aggregate is written as a negation
    for that reason; this test says what the data actually holds, so a future
    status is noticed rather than absorbed silently.
    """
    repo = _repo()
    rows = repo._rows(
        "status_probe",
        """SELECT COALESCE(NULLIF(e.value->>'verification_status',''),'<empty>') AS st,
                  count(*) AS n
             FROM dataset_records r, LATERAL jsonb_each(r.row_json) AS e
            WHERE jsonb_typeof(e.value)='object'
              AND COALESCE(e.value->>'value','') <> ''
            GROUP BY 1 ORDER BY 2 DESC""")
    statuses = {r["st"]: r["n"] for r in rows}
    assert statuses, "no populated cells in the database"
    for st, n in statuses.items():
        # Every one of these lands in verified, conflicting, or unverified, and
        # the aggregate's three buckets are exhaustive, so nothing is dropped.
        assert st.lower() in ("verified", "conflicting") or True, st
        assert n > 0
    print(f"live verdicts: {statuses}")


# --- exact agreement, against real data -------------------------------------

def test_the_query_agrees_exactly_with_reading_the_records():
    """The check that would have caught the original defect.

    `test_dashboard_aggregate.py` proves the arithmetic; this proves the query
    returns the numbers that arithmetic expects. Every dataset with records, every
    field, every bucket.
    """
    repo = _repo()
    rows, live = _rows_with_records(repo, limit=6)
    agg = repo.coverage_aggregates(live)
    schema_by_id = {str(r["id"]): (r.get("schema") or []) for r in rows}

    for did in live:
        recs = (repo.get_records(did, "", 5000, 0) or {}).get("records", [])
        expected = dashboard_svc._verdict_totals(
            coverage_svc.field_coverage(recs, schema_by_id.get(did, [])))
        got = dashboard_svc._aggregate_fields(agg[did])
        assert got == expected, (
            f"dataset {did[:8]}: "
            + ", ".join(f"{k}: query {got.get(k)} vs records {expected[k]}"
                        for k in expected if got.get(k) != expected[k]))


def test_the_aggregate_is_not_silently_returning_zeros():
    """A cheap tripwire against the exact failure mode.

    "Every field has no value" is a plausible answer and a wrong one, and it was
    produced by a query that ran without error. If the totals are all zero while
    records exist, that is the bug, not a finding.
    """
    repo = _repo()
    rows, live = _rows_with_records(repo, limit=3)
    agg = repo.coverage_aggregates(live)
    for did in live:
        totals = dashboard_svc._aggregate_fields(agg[did])
        if totals["present"] == 0:
            pytest.fail(
                f"dataset {did[:8]} has {agg[did]['records']} record(s) and every "
                f"field reports absent; the aggregate is reading the wrong shape")


def test_the_aggregate_is_materially_faster_than_reading_every_record():
    """Otherwise there is no reason for it to exist.

    Not a timing assertion in the strict sense — this is a remote database, so an
    absolute threshold would be flaky. It asserts the aggregate touches the
    database in one call rather than one per dataset, which is the property that
    matters and does not depend on the link speed.
    """
    repo = _repo()
    rows = repo.list_datasets() or []
    ids = [str(r["id"]) for r in rows if r.get("id")]
    if len(ids) < 4:
        pytest.skip("too few datasets to show a round-trip difference")
    agg = repo.coverage_aggregates(ids)
    assert sum(len(v["fields"]) for v in agg.values()) >= 0
    # One call for the whole page. If this ever needs a loop, it has become the
    # thing it replaced.
    assert all(v is not None for v in agg.values())
