"""Malformed ids are client mistakes (422), not outages (503).

A hand-typed or placeholder id (`DID`, `nope`, a truncated UUID) dies in
Postgres UUID parsing. Mapping that to E_DEPENDENCY sent readers restarting a
healthy server — twice on live runs. The guard below is deliberately narrow: it
matches only the invalid-syntax diagnostic, never a connection or constraint
failure, and an unparseable UUID can never match a row so nothing valid is
refused.
"""
from app.repositories.postgres_repo import _is_bad_uuid_error


def test_uuid_syntax_errors_are_recognised():
    assert _is_bad_uuid_error(Exception(
        'invalid input syntax for type uuid: "DID"')) is True
    assert _is_bad_uuid_error(Exception(
        'invalid input syntax for type uuid: "nope"')) is True


def test_other_failures_are_not_reclassified():
    assert _is_bad_uuid_error(Exception("connection refused")) is False
    assert _is_bad_uuid_error(Exception(
        'duplicate key value violates unique constraint "x"')) is False
    assert _is_bad_uuid_error(Exception("")) is False
