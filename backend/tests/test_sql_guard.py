"""The SQL a model writes must never be able to do more than read this dataset.

Most of these tests are about what gets *refused*. A guard that is only tested
on the queries it was written for is a guard whose holes have not been looked
at, and this grammar sits directly in front of a live database.
"""
import pytest

from app.services import sqlq

COLS = ["record_id", "company_name", "company_name__status", "industry",
        "industry__status", "founded", "batch_number"]
DID = "11111111-1111-1111-1111-111111111111"


# -- what must be allowed ----------------------------------------------------
@pytest.mark.parametrize("sql", [
    'SELECT * FROM records',
    'SELECT "company_name" FROM records',
    'SELECT "industry", count(*) AS n FROM records GROUP BY "industry"',
    "SELECT * FROM records WHERE \"company_name\" IS NOT NULL AND \"company_name\" <> ''",
    'SELECT * FROM records ORDER BY "founded" DESC LIMIT 20',
    'SELECT CAST("batch_number" AS integer) AS b FROM records',
    'SELECT lower("company_name") FROM records',
    'WITH x AS (SELECT * FROM records) SELECT count(*) FROM x',
    "SELECT DISTINCT \"industry\" FROM records",
    "SELECT * FROM records WHERE \"company_name\" ILIKE '%acme%'",
])
def test_ordinary_reads_are_allowed(sql):
    assert sqlq.validate(sql, COLS)


# -- what must be refused ----------------------------------------------------
@pytest.mark.parametrize("sql,needle", [
    ("DELETE FROM dataset_records", "delete"),
    ("UPDATE dataset_records SET row_json='{}'", "update"),
    ("DROP TABLE dataset_records", "drop"),
    ("SELECT 1; DROP TABLE dataset_records", "one statement"),
    ("SELECT * FROM records; SELECT pg_sleep(60)", "one statement"),
    ("TRUNCATE dataset_records", "truncate"),
    ("GRANT ALL ON dataset_records TO anon", "grant"),
    ("COPY dataset_records TO '/tmp/out.csv'", "copy"),
])
def test_writes_and_second_statements_are_refused(sql, needle):
    with pytest.raises(ValueError) as exc:
        sqlq.validate(sql, COLS)
    assert needle in str(exc.value).lower()


def test_another_table_is_refused_by_name():
    """An allowlist, not a blocklist: the model cannot reach a table we did
    not hand it, whether or not its name is on any list of bad words."""
    with pytest.raises(ValueError) as exc:
        sqlq.validate("SELECT * FROM users", COLS)
    assert "users" in str(exc.value)


def test_system_catalogs_are_refused():
    with pytest.raises(ValueError) as exc:
        sqlq.validate("SELECT current_user FROM records", COLS)
    assert "current_user" in str(exc.value)


@pytest.mark.parametrize("fn", ["pg_read_file", "lo_import", "dblink", "pg_sleep"])
def test_functions_that_reach_outside_the_database_are_refused(fn):
    with pytest.raises(ValueError) as exc:
        sqlq.validate(f"SELECT {fn}('x') FROM records", COLS)
    assert fn in str(exc.value)


def test_a_comment_cannot_hide_a_write():
    """Naive filters strip comments first or not at all. Here comments are
    removed by the tokenizer, so the write is still seen and refused."""
    with pytest.raises(ValueError):
        sqlq.validate("SELECT * FROM records /* harmless */ ; DROP TABLE users", COLS)


def test_a_quoted_string_containing_a_write_word_is_allowed():
    """The reverse error: a legal query filtering on the text 'deleted' must
    not be refused just because a forbidden word appears in a string literal."""
    assert sqlq.validate(
        "SELECT * FROM records WHERE \"company_name\" = 'deleted'", COLS)


def test_a_query_with_no_sql_is_refused():
    with pytest.raises(ValueError):
        sqlq.validate("   ", COLS)


def test_a_query_must_start_with_select_or_with():
    with pytest.raises(ValueError) as exc:
        sqlq.validate("EXPLAIN SELECT 1", COLS)
    assert "select" in str(exc.value).lower()


# -- the relation -----------------------------------------------------------
def test_the_relation_exposes_only_this_dataset_and_its_columns():
    sql, cols = sqlq.relation_sql(DID, [{"name": "company_name"}, {"name": "founded"}])
    assert "record_id" in cols
    assert "company_name" in cols and "company_name__status" in cols
    assert "dataset_records" in sql
    assert DID in sql
    # No other table is reachable from here.
    for other in ("users", "runs", "seen_fingerprints", "usage_ledger"):
        assert other not in sql


def test_the_relation_extracts_the_value_not_the_whole_cell():
    """Cells are objects. Reading the bare key returned the object serialised
    as text, so every row looked unique and a GROUP BY counted 1 for
    everything — an answer that looks like data and is not."""
    sql, _ = sqlq.relation_sql(DID, [{"name": "industry"}])
    assert "row_json -> 'industry' ->> 'value'" in sql
    assert "->> 'verification_status'" in sql
    # The older scalar shape still resolves, but only for cells that really
    # are scalars. An unconditional fallback returned the whole cell as text
    # for exactly those rows whose value was null — the emptiest rows produced
    # the longest strings.
    assert "jsonb_typeof" in sql
    assert "= 'string'" in sql


def test_a_quoted_literal_cannot_escape_the_key():
    sql, _ = sqlq.relation_sql(DID, [{"name": "it's"}])
    assert "'it''s'" in sql


def test_relation_quotes_awkward_column_names():
    sql, cols = sqlq.relation_sql(DID, [{"name": 'we"ird'}])
    assert 'we""ird' in sql
    assert 'we"ird' in cols


def test_dataset_id_cannot_be_escaped_with_a_quote():
    sql, _ = sqlq.relation_sql("abc' OR '1'='1", [])
    assert "' OR '1'='1" not in sql
    assert "abc" in sql


# -- the wrapper ------------------------------------------------------------
def test_the_wrapper_recaps_the_limit_however_large_the_model_asked():
    inner = "SELECT * FROM records LIMIT 999999"
    out = sqlq.build_query(inner, "SELECT 1", 50)
    assert out.endswith("LIMIT 50")
    # The model's own limit is inside the subquery and cannot raise the cap.
    assert out.count("LIMIT") == 2


def test_a_zero_or_negative_limit_is_clamped_to_one():
    assert sqlq.build_query("SELECT 1 FROM records", "x", 0).endswith("LIMIT 1")
    assert sqlq.build_query("SELECT 1 FROM records", "x", -5).endswith("LIMIT 1")


# -- the prompt -------------------------------------------------------------
def test_a_literal_percent_survives_to_execution(monkeypatch):
    """`ILIKE '%acme%'` is the ordinary way to answer "companies containing X".

    Passing an empty params tuple still puts psycopg into placeholder mode, and
    it reads every `%` as one — so the query died with "only '%s', '%b', '%t'
    are allowed as placeholders" on any search term, which is most questions
    about a specific name.
    """
    from app.repositories import postgres_repo

    seen = {}

    class _Cur:
        def execute(self, sql, params=None):
            seen["sql"] = sql
            seen["params"] = params
            if "fetchall" in sql:
                return None

        def fetchall(self):
            return [{"company_name": "Acme"}]

    repo = object.__new__(postgres_repo.PostgresRepo)
    monkeypatch.setattr(postgres_repo.PostgresRepo, "_run",
                        lambda self, op, fn: fn(_Cur()))
    sql = sqlq.build_query('SELECT * FROM records WHERE "company_name" ILIKE \'%acme%\'',
                           "rel", 10)
    rows = postgres_repo.PostgresRepo.run_readonly_sql(repo, sql)
    assert rows == [{"company_name": "Acme"}]
    assert "%acme%" in seen["sql"]
    # No params object at all, so nothing is interpolated and the literal stays.
    assert seen["params"] is None


def test_readonly_sql_declares_the_transaction_read_only(monkeypatch):
    from app.repositories import postgres_repo

    stmts = []

    class _Cur:
        def execute(self, sql, params=None):
            stmts.append(sql)

        def fetchall(self):
            return []

    repo = object.__new__(postgres_repo.PostgresRepo)
    monkeypatch.setattr(postgres_repo.PostgresRepo, "_run",
                        lambda self, op, fn: fn(_Cur()))
    postgres_repo.PostgresRepo.run_readonly_sql(repo, "SELECT 1")
    assert any("SET TRANSACTION READ ONLY" in s for s in stmts)


def test_the_prompt_lists_the_columns_and_forbids_everything_else():
    p = sqlq.build_prompt("which industries", [{"name": "industry"}], 42,
                          sqlq.DEFAULT_EXAMPLES)
    assert "industry" in p and "42 rows" in p
    assert "SELECT only" in p
    assert "No other tables" in p
    # It must not leak the underlying table name into an example the model
    # could copy verbatim.
    assert "FROM dataset_records" not in p
