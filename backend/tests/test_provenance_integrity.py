"""Stored cells must keep the provenance the pipeline built for them.

Three defects in this family shipped into production data, all the same shape: a
value computed carefully, carried across a boundary, and dropped by whatever did
not know about it.

1. `main._emit` hand-enumerated 9 of the 11 counters `provenance.summarize`
   produces, so `fields_unverified` and `fields_conflicting` were always 0.
2. `ProvenanceField` declared only `value`, `verification_status` and `source`.
   `validator._receipt_for` built `read_method`, `confidence`, `certainty` and
   three label fields; `RecordRow.model_validate` is the last step before a row is
   stored, and pydantic drops what a model does not declare. Every stored cell
   therefore lost the record of which route actually produced the value.
3. `_proxy_verdict` accepted a `throttled` flag and ignored it, so a throttled
   judge reported as absent.

These tests assert the *whole* pipeline output survives its boundaries, rather
than checking each boundary in isolation — because each of the three was caught
by reading the code, not by any test that existed.

A live run made this concrete: 12 pages fetched, 0 failed, real values extracted
with real quotes — and every cell stored with `read_method: None`.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.schemas.evidence import locate_quote
from app.schemas.run import Counters, RecordRow
from app.services import confidence as conf_svc
from app.services import provenance, validator
from app.services.validator import JudgeBudget


SOURCE = {"url": "https://example.test/a", "title": "A",
          "quote": "", "retrieved_at": "2026-01-01T00:00:00Z",
          "content_hash": "", "reference_id": "", "page_id": "p1",
          "start": None, "end": None}


def _row(**overrides) -> dict:
    cell = {"value": "Acme", "verification_status": "verified",
            "source": dict(SOURCE), **overrides}
    return {"fields": {"company_name": cell}}


def run(coro):
    import asyncio
    return asyncio.run(coro)


# ------------------------------------------------- the receipt survives storage


def test_a_stored_cell_keeps_its_read_method():
    """The defect: pydantic dropped every field ProvenanceField did not declare."""
    receipt = conf_svc.receipt("api", verified=True)
    cell = _row(**receipt)
    out = RecordRow.model_validate(cell).model_dump()["fields"]["company_name"]

    assert out.get("read_method"), (
        "the route that produced a value was dropped before storage; every "
        "stored cell reports how it arrived as nothing")
    assert "confidence" in out
    assert "certainty" in out
    assert "method_label" in out


def test_the_full_receipt_round_trips_through_wrap_record():
    """End to end through the function that builds a stored row.

    `raw["fields"]` holds plain values and `raw["evidence"]` holds the receipts —
    that is the shape the extractor produces. Putting a cell dict under
    `fields` instead makes the value a dict, which is exactly the mistake the
    `jsonrung` rung made and why it is worth spelling the contract out here.
    """
    fields_spec = [{"name": "company_name", "type": "string", "required": True,
                    "description": "Name"}]
    raw = {"fields": {"company_name": "Acme"},
           "evidence": [{"field": "company_name", "value": "Acme",
                         "quote": "Acme Corp is a company",
                         "source_url": "https://example.test/a",
                         "read_method": "api", "reference_id": ""}]}
    text = "Acme Corp is a company"

    out = run(validator.wrap_record(
        fields_spec, raw, text, "https://example.test/a", "A",
        now="2026-01-01T00:00:00Z", page_id="p1", budget=JudgeBudget(0)))
    cell = out["company_name"]

    assert cell.get("value") == "Acme"
    assert cell.get("read_method") == "api", (
        f"a deterministic route was flattened to {cell.get('read_method')!r}; "
        f"the record no longer says where its value came from")
    assert cell.get("verification_status") == "verified"
    assert cell.get("source", {}).get("page_id") == "p1"


def test_a_model_routed_cell_is_labelled_llm_not_blank():
    fields_spec = [{"name": "company_name", "type": "string", "required": True,
                    "description": "Name"}]
    raw = {"fields": {}, "evidence": [{"field": "company_name", "value": "Acme",
                                       "quote": "Acme Corp is a company",
                                       "source_url": "https://example.test/a",
                                       "reference_id": ""}]}
    out = run(validator.wrap_record(
        fields_spec, raw, "Acme Corp is a company", "https://example.test/a", "A",
        now="2026-01-01T00:00:00Z", page_id="p1", budget=JudgeBudget(0)))
    assert out["company_name"].get("read_method") == "llm"


# -------------------------------------------------------- the counters survived


def test_every_counter_summarize_produces_reaches_the_model():
    rows = [{"fields": {
        "a": {"value": "1", "verification_status": "verified"},
        "b": {"value": None, "verification_status": "unverified"},
    }}]
    summary = provenance.summarize(rows)
    for key in summary:
        assert key in Counters.model_fields, (
            f"provenance.summarize produces {key!r}, Counters does not declare "
            f"it, and pydantic drops it from every run report")


def test_the_two_always_zero_counters_are_populated():
    """`fields_unverified` and `fields_conflicting` were always reported as 0."""
    rows = [{"fields": {
        "a": {"value": "1", "verification_status": "verified"},
        "b": {"value": None, "verification_status": "unverified"},
        "c": {"value": None, "verification_status": "unverified"},
        "d": {"value": None, "verification_status": "unverified"},
    }}]
    summary = provenance.summarize(rows)
    assert summary["fields_unverified"] >= 3, (
        "three unverified cells reported as zero")
    assert "fields_conflicting" in summary


# ------------------------------------------------------ throttling is not absence


def test_a_throttled_judge_is_reported_as_throttled(real_judge):
    """`_proxy_verdict(throttled=...)` used to ignore the flag entirely.

    `real_judge` undoes the autouse judge stub, so the real transport runs — the
    stub answers immediately and would hide exactly the failure being tested.
    """
    from app.providers.llm import zen

    async def throttled(state, questions, **kw):
        raise zen.JevRateLimited("429")

    real = zen.systemone
    zen.systemone = throttled
    try:
        out = run(validator.jev.evidence_verification(
            "a company worth 5 million",
            "The company is valued at 5 million",
            "The company is valued at 5 million in a recent round."))
    finally:
        zen.systemone = real

    assert out["judgment"] == "RATE_LIMITED", (
        "a throttled judge must be distinguishable from an absent one; the "
        "operator response is different — retry later, versus fix something")
    assert out["provider"] == "throttled"


# -------------------------------------------------------------- the quote holds


def test_a_stored_quote_resolves_against_the_stored_text():
    """The other half of "is this actually supported".

    A cell whose quote cannot be located in the document it claims to come from
    is not evidence, whatever its verification status says.
    """
    text = "Acme Corp raised 4 million in a Series A round."
    assert locate_quote("Acme Corp", text)[0] is not None
    # A quote that is not in the document yields a (None, None) span, which is
    # what the gate treats as "not locatable" — not an exception.
    assert locate_quote("a company that does not exist here", text)[0] is None


def test_confidence_prior_orders_the_routes_sensibly():
    """A structured feed outranks a model, which outranks a search snippet."""
    assert (conf_svc.METHOD_PRIOR["api"] > conf_svc.METHOD_PRIOR["labelled"]
            > conf_svc.METHOD_PRIOR["llm"] > conf_svc.METHOD_PRIOR["search"])
    assert conf_svc.field_confidence("api", verified=False) == 0.0, (
        "an unchecked value scores nothing, not 'something less than nothing'")