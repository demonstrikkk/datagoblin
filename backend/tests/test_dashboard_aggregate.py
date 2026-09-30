"""The dashboard's coverage numbers must not depend on how they were computed.

Two implementations now exist for the same question. `PostgresRepo.coverage_aggregates`
counts verdict statuses inside the database and returns a dozen integers per
dataset. `_compute` can instead read records into the process and call
`coverage.field_coverage`. They must agree exactly.

They disagreeing would be the worst kind of bug in this system: not a crash, not a
blank page, but a number that changes depending on which adapter is live or
whether the aggregate happened to succeed. A coverage figure that is a property of
deployment rather than of data is not a measurement, and this project's whole claim
is that its figures are measurements.

The cases below are the ones where the two implementations genuinely differ in
approach, and therefore the ones where they can genuinely disagree:

* a field whose value is a bare scalar, stored before provenance existed
* a field present with a status of `not_proven`, which `field_coverage` folds into
  `unverified` and the SQL has to fold in the same place
* a declared schema field that appears in no record, which only the SQL path can
  know about without reading the records
* a dataset with no records at all
"""
from __future__ import annotations

import inspect
import re

from app.services import coverage as coverage_svc
from app.services import dashboard as dashboard_svc


def _cell(value, status):
    return {"value": value, "verification_status": status,
            "source": {"url": "https://x.test/p", "title": "t", "quote": "q"}}


def _expected(records, schema):
    """What the in-process path says, using the one shared implementation."""
    matrix = coverage_svc.field_coverage(records, schema)
    return dashboard_svc._verdict_totals(matrix)


class _Store:
    """A store that records how many times its records were read."""

    def __init__(self, datasets, records_by_id, runs=None):
        self._datasets = datasets
        self._records = records_by_id
        self._runs = runs or {}
        self.record_reads = 0

    def list_datasets(self, limit=50):
        return list(self._datasets)

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        self.record_reads += 1
        recs = self._records.get(dataset_id, [])
        return {"records": recs, "total": len(recs)}

    def get_run(self, run_id):
        return self._runs.get(run_id, {})

    def get_sources(self, dataset_id):
        return {"sources": []}

    def get_pages(self, run_id, limit=200):
        return []

    # The aggregate, computed with the shared function so the test compares the
    # *plumbing* (does the dashboard read the right numbers?) rather than
    # re-testing the SQL, which needs a live Postgres.
    def coverage_aggregates(self, dataset_ids):
        out = {}
        for did in dataset_ids:
            recs = self._records.get(did, [])
            row = next(r for r in self._datasets if r["id"] == did)
            matrix = coverage_svc.field_coverage(recs, row.get("schema") or [])
            out[did] = {"records": matrix["records"], "fields": {
                f["field"]: {k: f[k] for k in
                             ("present", "missing", "records", "verified",
                              "unverified", "conflicting")}
                for f in matrix["fields"]}}
        return out


def _both(records, schema, runs=None):
    """The aggregate's totals and the record-reading totals, side by side."""
    datasets = [{"id": "d1", "run_id": "r1", "name": "n", "schema": schema,
                 "record_count": len(records)}]
    recs_by_id = {"d1": records}
    agg_store = _Store(datasets, recs_by_id, runs)
    out = dashboard_svc._compute(agg_store, 25)
    return out["datasets"][0]["coverage"], _expected(records, schema)


# --- agreement ---------------------------------------------------------------

def test_a_plain_dataset_agrees_both_ways():
    records = [
        {"record_id": "1", "fields": {"a": _cell("x", "verified"), "b": _cell("y", "unverified")}},
        {"record_id": "2", "fields": {"a": _cell("z", "verified"), "c": _cell("1", "verified")}},
    ]
    schema = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    agg, exp = _both(records, schema)
    assert agg == exp


def test_a_bare_scalar_from_before_provenance_agrees():
    """Stored as a plain string, so it is a value with no verdict."""
    records = [
        {"record_id": "1", "fields": {"a": "plain", "b": _cell("y", "verified")}},
        {"record_id": "2", "fields": {"a": "also plain"}},
    ]
    schema = [{"name": "a"}, {"name": "b"}]
    agg, exp = _both(records, schema)
    assert agg == exp
    assert agg["unverified"] == 2, "a scalar is a value with no verdict, not absent"


def test_not_proven_is_folded_into_unverified_both_ways():
    records = [
        {"record_id": "1", "fields": {"a": _cell("x", "not_proven")}},
        {"record_id": "2", "fields": {"a": _cell("y", "verified")}},
    ]
    agg, exp = _both(records, [{"name": "a"}])
    assert agg == exp
    assert agg["unverified"] == 1


def test_a_declared_field_no_record_carries_agrees():
    """The gap the coverage view exists to show must not vanish."""
    records = [{"record_id": "1", "fields": {"a": _cell("x", "verified")}}]
    schema = [{"name": "a"}, {"name": "never_extracted"}]
    agg, exp = _both(records, schema)
    assert agg == exp
    assert agg["empty_fields"] == 1


def test_a_dataset_with_no_records_agrees():
    """Agreement is the subject; the shared implementation's answer is 0 cells.

    `field_coverage` gives a declared field `missing = 0` when there are no
    records, because "missing on 0 of 0 records" has no denominator and it guards
    the division. So a schema with no records contributes no cells.

    That is a real and separate weakness — a dataset with fifteen declared fields
    and zero records reports fifteen fields, zero outstanding, and an empty gap
    queue, which reads as complete. It is *not* introduced here, both paths have
    always said this, and the honest fix is a distinct condition ("this dataset
    declares a schema and holds no records") rather than bending `missing` to a
    case its arithmetic cannot express. Left alone deliberately, and asserted so
    a future change to it is a decision rather than an accident.
    """
    agg, exp = _both([], [{"name": "a"}, {"name": "b"}])
    assert agg == exp
    assert agg["fields"] == 2
    assert agg["cells"] == 0
    assert agg["present"] == 0


def test_a_partially_filled_field_is_counted_as_partial_both_ways():
    records = [
        {"record_id": "1", "fields": {"a": _cell("x", "verified")}},
        {"record_id": "2", "fields": {}},
    ]
    agg, exp = _both(records, [{"name": "a"}])
    assert agg == exp
    assert agg["partial_fields"] == 1


def test_conflicts_are_not_double_counted_as_unverified():
    records = [
        {"record_id": "1", "fields": {"a": _cell("x", "conflicting")}},
        {"record_id": "2", "fields": {"a": _cell("y", "verified")}},
    ]
    agg, exp = _both(records, [{"name": "a"}])
    assert agg == exp
    assert agg["conflicting"] == 1
    assert agg["unverified"] == 0


# --- the aggregate is actually used -------------------------------------------

def test_the_dashboard_does_not_read_records_when_the_aggregate_answers():
    """The whole point. A per-dataset records read is 25 round trips of JSON."""
    records = [{"record_id": "1", "fields": {"a": _cell("x", "verified")}}]
    store = _Store([{"id": f"d{i}", "run_id": "r1", "name": f"n{i}",
                     "schema": [{"name": "a"}], "record_count": 1} for i in range(5)],
                   {f"d{i}": records for i in range(5)})
    dashboard_svc._compute(store, 25)
    assert store.record_reads == 0, \
        f"records were read {store.record_reads} times despite a working aggregate"


def test_a_failed_aggregate_falls_back_rather_than_emptying_the_page():
    """A latency regression is acceptable; losing the dashboard is not."""

    class _Broken(_Store):
        def coverage_aggregates(self, dataset_ids):
            raise RuntimeError("database went away")

    records = [{"record_id": "1", "fields": {"a": _cell("x", "verified")}}]
    store = _Broken([{"id": "d1", "run_id": "r1", "name": "n",
                      "schema": [{"name": "a"}], "record_count": 1}], {"d1": records})
    out = dashboard_svc._compute(store, 25)
    assert out["datasets"][0]["coverage"]["present"] == 1
    assert out["unreadable_datasets"] == 0


def test_a_partial_run_is_reported_once_per_run_not_once_per_dataset():
    """The run is the source of the fact; reading it per dataset was a round trip
    each for a value that could not differ."""
    records = [{"record_id": "1", "fields": {"a": _cell("x", "verified")}}]
    store = _Store([{"id": f"d{i}", "run_id": "shared", "name": f"n{i}",
                     "schema": [{"name": "a"}], "record_count": 1} for i in range(4)],
                   {f"d{i}": records for i in range(4)},
                   runs={"shared": {"status": "PARTIAL", "partial": True}})
    reads = []
    real = store.get_run

    def _counting(run_id):
        reads.append(run_id)
        return real(run_id)

    store.get_run = _counting
    out = dashboard_svc._compute(store, 25)
    assert len(reads) == 1, f"the same run was read {len(reads)} times"
    assert all(d["partial"] for d in out["datasets"])


def test_a_reaped_run_does_not_remove_the_dataset():
    """The dataset is still on disk, so the dashboard must still show it."""

    class _Reaped(_Store):
        def get_run(self, run_id):
            raise RuntimeError("run was reaped")

    records = [{"record_id": "1", "fields": {"a": _cell("x", "verified")}}]
    store = _Reaped([{"id": "d1", "run_id": "gone", "name": "n",
                      "schema": [{"name": "a"}], "record_count": 1}], {"d1": records})
    out = dashboard_svc._compute(store, 25)
    assert len(out["datasets"]) == 1
    assert out["datasets"][0]["partial"] is False


# --- the SQL itself ---------------------------------------------------------
# `test_dashboard_sql_live.py` runs the real query, but it skips without a
# database and the suite deliberately never loads `.env` — so it would not have
# caught the original defect in normal use.
#
# That defect was a single wrong JSON path: `row_json -> 'fields'` instead of
# `row_json`. The query was valid, returned rows, and reported every dataset as
# empty while looking entirely healthy. Nothing about executing it detects that;
# only reading the path does. So the path is asserted here, hermetically, and it
# costs nothing to keep.

def test_the_aggregate_reads_the_stored_shape_and_not_an_imagined_one():
    """`row_json` is the field map itself.

    `get_records` presents it as `{"record_id": ..., "fields": row_json}`, so the
    `fields` key appears in every fixture and every API response and reaching for
    `row_json -> 'fields'` looks right. It is not: the stored top level is
    `{"company_name": {...}, "country": {...}}`, that path is NULL for every row,
    and the query silently reports everything as absent.

    Asserted against the SQL as executed, not the method source — the docstring
    names the wrong path deliberately, to explain this very bug, and a substring
    check over the source would trip on the explanation.
    """
    sql = _executed_sql()

    assert "-> 'fields'" not in sql, (
        "coverage_aggregates reads row_json->'fields', but row_json IS the field "
        "map, so this makes jsonb_each yield nothing and every dataset reads empty")

    assert "jsonb_each(" in sql
    assert re.search(r"jsonb_each\(\s*CASE WHEN jsonb_typeof\(r\.row_json\)", sql), \
        "expected jsonb_each over r.row_json itself"


def test_unverified_is_a_negation_so_a_new_status_still_lands_somewhere():
    """`judgment_unavailable` is 992 cells here and a literal list reported zero.

    `coverage._cell_value` falls back to "unverified" for any status it does not
    recognise. Restating that as an IN-list means a new status is dropped on the
    floor, and the failure is invisible: cells that should be counted simply are
    not.
    """
    sql = _executed_sql()
    assert re.search(r"NOT IN \('verified','conflicting'\)", sql), (
        "unverified is not expressed as a negation of the other two verdicts, so "
        "an unrecognised status would be counted as neither verified, conflicting "
        "nor unverified")


def test_a_stored_json_null_is_absent_not_unverified():
    """A null value claims the source did not carry it, which is not what happened."""
    sql = _executed_sql()
    assert "NULLIF(e.value->>'value', '')" in sql, \
        "a stored JSON null must be treated as absent"
    assert "WHERE NOT empty" in sql, \
        "empty cells must be filtered out before the verdict counts"


def test_a_declared_field_with_no_record_still_appears():
    """"Declared and never extracted" is the gap the coverage view exists to show."""
    sql = _executed_sql()
    assert "jsonb_array_elements" in sql and "NOT EXISTS" in sql, (
        "schema fields absent from every record must be added at zero, or they "
        "vanish from an aggregate that only looks at records")


def test_the_query_reads_each_dataset_once():
    """One call for the page. A loop here is the thing it replaced."""
    src = inspect.getsource(dashboard_svc._compute)
    assert src.count("coverage_aggregates") == 1
    assert "aggregator([" in src, "the aggregate must be called once with the page"


# --- helpers -----------------------------------------------------------------

def _executed_sql() -> str:
    """The SQL text handed to the cursor, captured without a database.

    The real method is invoked with `_rows` replaced. `_rows` is the single choke
    point every Postgres read goes through, so this sees exactly what would have
    been sent — and needs no server, which matters because the suite deliberately
    never loads `.env` and so never has a `DATABASE_URL` to connect with.
    """
    from app.repositories.postgres_repo import PostgresRepo

    captured: dict = {}

    class _Stub:
        def _rows(self, op, sql, params=()):
            captured["sql"] = sql
            return []

    PostgresRepo.coverage_aggregates(_Stub(), ["00000000-0000-0000-0000-000000000001"])
    assert "sql" in captured, "the method issued no query"
    return captured["sql"]
