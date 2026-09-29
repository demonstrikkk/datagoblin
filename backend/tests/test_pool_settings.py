"""Postgres connection settings that the pooler makes mandatory.

These are not preferences. A transaction-mode pooler hands one backend to
different clients, and each of these settings exists because leaving it at its
default produced a failure that looked like an outage rather than a
misconfiguration.
"""
import pytest

from app.repositories import postgres_repo


def _pool_kwargs(monkeypatch) -> dict:
    """Build the pool for real, and return the kwargs it was opened with."""
    captured: dict = {}

    class _Pool:
        def __init__(self, **kw):
            captured.update(kw)

    import psycopg_pool
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _Pool)
    repo = object.__new__(postgres_repo.PostgresRepo)
    repo._pool = None
    repo._lock = __import__("threading").Lock()
    repo._dsn = "postgresql://u:p@host/db"
    repo._connect_timeout = 5
    repo._get_pool()
    return captured


def test_prepared_statements_are_disabled(monkeypatch):
    """Without this, reading pages fails intermittently.

    psycopg auto-prepares a statement after it has run twice on a connection
    and then EXECUTEs it, which requires a session-scoped prepared statement.
    PgBouncer in transaction mode hands the same backend to different clients,
    so one client's `_pg3_0` collides with another's and the query fails with
    `DuplicatePreparedStatement`. Observed as 503s from `get_page` after about
    a dozen sequential reads, which is exactly the kind of failure that gets
    blamed on the network.
    """
    kw = _pool_kwargs(monkeypatch)
    assert kw["kwargs"].get("prepare_threshold") is None, (
        "prepared statements are on; a transaction-mode pooler will collide")


def test_the_row_factory_is_still_set_on_the_pooled_connections(monkeypatch):
    """It belongs to the pool, not to each `connect()`. Set per connect, reads
    come back as tuples and every `row["col"]` raises."""
    kw = _pool_kwargs(monkeypatch)
    from psycopg.rows import dict_row
    assert kw["kwargs"]["row_factory"] is dict_row


def test_the_pool_stays_small_and_does_not_hold_a_remote_connection_forever(monkeypatch):
    kw = _pool_kwargs(monkeypatch)
    assert kw["max_size"] == 6
    assert kw["max_lifetime"] == 1800


def test_a_pool_that_cannot_open_becomes_a_typed_error(monkeypatch):
    """A dead database must not surface as a raw driver exception from deep
    inside a request; it has to be an E_DEPENDENCY the caller can report."""
    import psycopg_pool
    from app.core.errors import AppError

    def _boom(**_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _boom)
    repo = object.__new__(postgres_repo.PostgresRepo)
    repo._pool = None
    repo._lock = __import__("threading").Lock()
    repo._dsn = "postgresql://u:p@host/db"
    repo._connect_timeout = 1
    with pytest.raises(AppError) as exc:
        repo._get_pool()
    assert exc.value.code == "E_DEPENDENCY"

