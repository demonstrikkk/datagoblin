"""The run report must not silently report zeroes, and the contract must cover
every route the code serves.

D3 and D4 were the same mistake twice: a value was computed carefully, stored,
and then dropped at the last step by a model that did not declare a field for it.
`GET /api/runs/{id}` reported `verified: 0, needs_review: 0` for every run in the
database, and could not report `partial` or `no_yield_reason` at all — a run that
stored only what it verified was indistinguishable from a clean one.

These tests pin the shape, so a future field cannot be added to the emitter and
forgotten at the model.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from app.schemas.run import Counters, RunView

CONTRACT = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "api.openapi.yaml"


def test_counters_declare_every_key_the_event_handler_builds():
    """`main._emit` builds the source dict; the model is the consumer.

    Read from the code rather than a hand-copied list, so a new counter that the
    emitter starts producing cannot be missing here without this failing.
    """
    main_src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8")
    start = main_src.find('counters = {"attempted"')
    assert start > 0, "the counters dict in main._emit moved; update this test"
    end = main_src.find("}", start)
    block = main_src[start:end]

    import re
    emitted = set(re.findall(r'"([a-z_]+)"\s*:', block))
    emitted -= {"attempted", "successful", "failed", "records"}  # appear twice
    declared = set(Counters.model_fields)
    assert emitted, "could not read the emitted counter keys from main.py"
    assert emitted <= declared, (
        f"main._emit produces {sorted(emitted - declared)} and Counters does not "
        f"declare them, so pydantic drops them and the API reports zero")


def test_every_emitted_counter_survives_the_model():
    c = Counters(attempted=12, successful=12, failed=0, records=29,
                 fields_verified=40, fields_judgment_unavailable=80,
                 records_fully_verified=0, records_needing_review=29)
    d = c.model_dump()
    assert d["fields_verified"] == 40
    assert d["fields_judgment_unavailable"] == 80
    assert d["records_needing_review"] == 29


def test_record_and_field_counts_are_not_conflated():
    """`verified` used to mean both. A run with 29 records and 40 proven fields
    must be able to say so."""
    c = Counters(records=29, verified=29, fields_verified=40)
    d = c.model_dump()
    assert d["records"] == 29 and d["verified"] == 29
    assert d["fields_verified"] == 40


def test_run_view_can_report_partial_and_no_yield_reason():
    v = RunView(run_id="r", status="PARTIAL", partial=True,
                no_yield_reason="a stage overran the runtime budget")
    d = v.model_dump()
    assert d["partial"] is True
    assert "overran" in d["no_yield_reason"]


def test_a_clean_run_is_not_marked_partial():
    d = RunView(run_id="r", status="COMPLETED").model_dump()
    assert d["partial"] is False
    assert d["no_yield_reason"] == ""


# --- the contract covers what the code serves --------------------------------

def test_the_openapi_contract_parses():
    assert yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_every_registered_route_is_in_the_contract():
    """A route with no contract entry is a route nobody has described.

    `/api/datasets/{did}/profile` was missing for its whole life: 24 tests, the
    dashboard and the Coverage view all depended on it, and the contract — which
    claimed to be authoritative — did not mention it.
    """
    main_src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8")
    import re
    routes = set()
    for m in re.finditer(r'@app\.(?:get|post|put|delete|patch)\("([^"]+)"', main_src):
        path = m.group(1).replace("{did}", "{did}").replace("{run_id}", "{run_id}")
        routes.add(path)

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    documented = set((spec.get("paths") or {}))

    missing = sorted(p for p in routes if p not in documented)
    assert not missing, f"registered but undocumented: {missing}"
