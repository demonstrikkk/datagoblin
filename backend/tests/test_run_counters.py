"""Counters must survive the trip from `provenance.summarize` to the API.

`provenance.summarize` returns 11 counters. The SSE handler in `main.py` copied
9 of them by hand, so `fields_unverified` and `fields_conflicting` were computed
and then dropped. Because both are *meaningful* rather than decorative — they are
the two numbers that say "how much of this is actually proven" — every run in the
database reported 0 for both. A dataset that was 80% unverified read as 100%
verified.

Enumerating by hand is the defect, not the omission. These tests assert the three
lists agree structurally, so a thirteenth counter cannot be added in one place and
silently vanish in another.

Membership is checked as a set because `verified`/`needs_review` are declared on
the model for backwards compatibility and are not emitted by `summarize`; the
model may be a superset, but the handler must be a superset of `summarize`.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from app.schemas.run import Counters
from app.services import provenance

MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"


def _summarise_keys() -> set[str]:
    """The literal keys `provenance.summarize` returns, read from its source.

    Read statically rather than by calling it, so the test keeps working with an
    empty record list (which returns a different, short dict).
    """
    tree = ast.parse(inspect.getsource(provenance))
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "fields_verified" in keys:
                return keys
    raise AssertionError("could not find the dict provenance.summarize returns")


def _handler_keys() -> set[str]:
    """The keys `main._emit` copies off the event's `data`."""
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "counters"
                   for t in node.targets):
            continue
        # New style: `counters = {key: ... for key in ("a", "b", ...)}`
        if isinstance(node.value, ast.DictComp):
            src = node.value.generators[0].iter
            if isinstance(src, (ast.Tuple, ast.List, ast.Set)):
                return {e.value for e in src.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        # Old style: an explicit dict literal.
        if isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("could not find the `counters` assignment in main.py")


def test_handler_copies_every_counter_summarize_produces() -> None:
    missing = _summarise_keys() - _handler_keys()
    assert not missing, (
        f"main._emit drops {sorted(missing)}: provenance.summarize computes them "
        f"and they never reach the run view, so the API reports 0 forever")


def test_counters_model_declares_everything_the_handler_copies() -> None:
    """A key pydantic does not declare is silently dropped on the way out."""
    modelled = set(Counters.model_fields)
    missing = _handler_keys() - modelled
    assert not missing, (
        f"main._emit builds {sorted(missing)} but Counters does not declare them, "
        f"so pydantic drops them from GET /api/runs/{{id}}")


def test_the_two_dropped_counters_are_back() -> None:
    """The specific regression, named so it cannot be quietly reintroduced."""
    modelled = set(Counters.model_fields)
    for key in ("fields_unverified", "fields_conflicting"):
        assert key in _summarise_keys(), f"{key} no longer produced by summarize"
        assert key in _handler_keys(), f"{key} no longer copied by main._emit"
        assert key in modelled, f"{key} no longer declared on Counters"


def test_counters_accept_a_full_provenance_payload() -> None:
    """End to end through the model, with every key populated."""
    summary = provenance.summarize([
        {"fields": {"a": {"value": "1", "verification_status": "verified"},
                    "b": {"value": None, "verification_status": "unverified"},
                    "c": {"value": "x", "verification_status": "judgment_unavailable"},
                    "d": {"value": "y", "verification_status": "rate_limited"}}},
    ])
    counters = Counters(**{k: summary[k] for k in _handler_keys() if k in summary})
    assert counters.fields_unverified >= 1, (
        "a record with an unverified field must register as unverified")
    assert counters.fields_verified >= 1
    assert counters.fields_judgment_unavailable >= 1


def test_counters_reject_negative_values() -> None:
    import pydantic

    try:
        Counters(fields_unverified=-1)
    except pydantic.ValidationError:
        return
    raise AssertionError("Counters accepted a negative count")