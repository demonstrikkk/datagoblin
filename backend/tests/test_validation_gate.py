"""Validation must be a gate, and must never claim a judgement it did not get.

The four defects this locks in, all measured:

  * No stage ever raised DropItem. ItemPipeline was fully built for it and the
    runner even looped over `dropped` - but nothing ever dropped, so a record
    whose every field failed was still written to the dataset with null values.
  * `required` and `type` were discarded outright (`_ = required, ftype`), and
    normalize only transforms a value without ever reporting failure, so a
    string where a number was declared passed as verified.
  * `evidence_verification` returned SUPPORTED/0.9 when no judge was reachable.
    A dead judge silently promoted every substring match to "verified", and the
    whole suite was passing on that basis without noticing.
  * The 0.4-0.6 confidence band is a real "cannot tell", and was treated as
    support.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.decision import jev  # noqa: E402
from app.services import provenance as provenance_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


PAGE = "Acme Corp was founded in 2011 and employs roughly 240 people."
QUOTE = "employs roughly 240 people"


def _raw(value, quote=QUOTE):
    return {"fields": {"headcount": value},
            "evidence": [{"field": "headcount", "value": value, "quote": quote}]}


# --- an unreachable judge is not a passing judge ---------------------------

def test_unreachable_judge_is_not_verified(no_judge):
    """The load-bearing fix. A judge that never ran must not be reported as one
    that ruled in favour.

    The value is deliberately NOT contained verbatim in the quote: when it is,
    support is deduced from the substring and no judge is needed at all (see
    `test_value_inside_its_own_quote_skips_the_judge`)."""
    v, st = run(validator_svc.verify_field(
        "a mid-sized firm", "employs roughly 240 people", PAGE, True, "string"))
    assert st == "judgment_unavailable"
    assert v == "a mid-sized firm", "the value is on the page; only the ruling is missing"


def test_unreachable_judge_verdict_says_so_explicitly(no_judge):
    out = run(jev.evidence_verification("a mid-sized firm", QUOTE, PAGE))
    assert out["judgment"] == "JUDGMENT_UNAVAILABLE"
    assert out["provider"] == "none"


def test_hesitant_judge_is_not_support(monkeypatch):
    """0.4-0.6 is the judge saying it cannot tell."""

    async def _hesitant(prompt, schema, **kw):
        return {"support": {"type": "noul", "noul": 0.5}}

    monkeypatch.setattr(jev, "_jev_call", _hesitant)
    v, st = run(validator_svc.verify_field(
        "a mid-sized firm", QUOTE, PAGE, True, "string"))
    assert st == "judgment_unavailable"


def test_supporting_judge_verifies(monkeypatch):
    called = {"n": 0}

    async def _yes(prompt, schema, **kw):
        called["n"] += 1
        return {"support": {"type": "noul", "noul": 0.9}}

    monkeypatch.setattr(jev, "_jev_call", _yes)
    v, st = run(validator_svc.verify_field(
        "a mid-sized firm", QUOTE, PAGE, True, "string"))
    assert (v, st) == ("a mid-sized firm", "verified")
    assert called["n"] == 1


def test_value_inside_its_own_quote_skips_the_judge(monkeypatch):
    """A value that literally appears in its quote makes the judge's question
    vacuous. 728 extracted records meant thousands of round trips asking it
    anyway, and a blown budget."""
    called = {"n": 0}

    async def _must_not_be_called(prompt, schema, **kw):
        called["n"] += 1
        return {"support": {"type": "noul", "noul": 0.0}}

    monkeypatch.setattr(jev, "_jev_call", _must_not_be_called)
    v, st = run(validator_svc.verify_field(240, QUOTE, PAGE, True, "number"))
    assert (v, st) == (240, "verified")
    assert called["n"] == 0, "a vacuous question must not be sent to a model"
    out = run(jev.evidence_verification("240", QUOTE, PAGE))
    assert out["provider"] == "deterministic"


# --- the judge budget -------------------------------------------------------

def test_judge_budget_is_bounded_and_reported_honestly(monkeypatch):
    """Past the cap the value is kept but never counted as verified."""
    called = {"n": 0}

    async def _yes(prompt, schema, **kw):
        called["n"] += 1
        return {"support": {"type": "noul", "noul": 0.9}}

    monkeypatch.setattr(jev, "_jev_call", _yes)
    monkeypatch.setattr(validator_svc, "JUDGE_BUDGET", validator_svc.JudgeBudget(3))
    statuses = []
    for i in range(8):
        _, st = run(validator_svc.verify_field(
            f"firm-{i}", "employs roughly 240 people", PAGE, False, "string"))
        statuses.append(st)
    assert called["n"] == 3, "the cap did not hold"
    assert statuses[:3] == ["verified"] * 3
    assert set(statuses[3:]) == {"judgment_unavailable"}


def test_unlimited_judge_budget(monkeypatch):
    monkeypatch.setattr(validator_svc, "JUDGE_BUDGET", validator_svc.JudgeBudget(0))
    b = validator_svc.JudgeBudget(0)
    assert b.exhausted is False
    for _ in range(1000):
        b.spend()
    assert b.exhausted is False, "0 must mean unlimited, not nothing"


# --- required and type are actually enforced --------------------------------

def test_type_mismatch_is_rejected():
    """`required` and `type` used to be discarded. A non-numeric string in a
    number field passed as verified."""
    v, st = run(validator_svc.verify_field(
        "not a number", "Acme Corp was founded in 2011", PAGE, True, "number"))
    assert (v, st) == (None, "unverified")


def test_unsupported_claim_is_still_rejected_when_no_judge(no_judge):
    """Failing closed must not mean failing everything: a quote that is not on
    the page is rejected deterministically, no judge needed."""
    v, st = run(validator_svc.verify_field(
        999, "employs roughly 999 people", PAGE, True, "number"))
    assert (v, st) == (None, "unverified")


@pytest.mark.parametrize("value,ftype,ok", [
    (240, "number", True),
    ("1,200", "number", True),
    ("$2.4B", "number", True),
    ("about 240", "number", False),
    (True, "number", False),
    ("Acme", "string", True),
    ("2024-01-31", "date", True),
    ("January 2024", "date", True),
    ("not a date", "date", False),
    ("https://acme.example", "url", True),
    ("acme.example", "url", False),
])
def test_type_gate(value, ftype, ok):
    assert validator_svc.type_ok(value, ftype) is ok


def test_empty_value_is_not_a_type_error():
    """Emptiness is `required`'s business; the type gate must not claim it."""
    assert validator_svc.type_ok("", "number") is True
    assert validator_svc.type_ok(None, "date") is True


# --- the record-level gate --------------------------------------------------

def test_record_missing_a_required_field_is_dropped():
    """ItemPipeline's drop path was dead code. A record that cannot satisfy its
    own required fields must not be written as a row of nulls."""
    from app.services import item_pipeline as pipeline_svc

    spec = [{"name": "company", "type": "string", "required": True}]
    wrapped = run(validator_svc.wrap_record(spec, {"fields": {}, "evidence": []},
                                            PAGE, "u", "t"))

    async def _stage(raw):
        if not wrapped.get("company", {}).get("value"):
            raise pipeline_svc.DropItem("required field(s) not evidenced: company",
                                        "validate")
        return {"fields": wrapped}

    pipe = pipeline_svc.ItemPipeline([("validate", _stage)], max_concurrency=2)
    kept, dropped, _ = run(pipe.run([{"fields": {}}]))
    assert kept == [] and len(dropped) == 1
    assert "company" in dropped[0]["reason"]


def test_optional_missing_field_does_not_drop_the_record():
    from app.services import item_pipeline as pipeline_svc

    async def _stage(raw):
        return {"fields": {"note": {"value": None, "verification_status": "unverified"}}}

    pipe = pipeline_svc.ItemPipeline([("validate", _stage)], max_concurrency=2)
    kept, dropped, _ = run(pipe.run([{"fields": {}}]))
    assert len(kept) == 1 and dropped == []


# --- honest counting --------------------------------------------------------

def test_summary_counts_records_and_fields_separately():
    """`{"records":1,"verified":0,"needs_review":4}` reads as four bad records
    when it is one record with four unproven fields."""
    rows = [{"fields": {"a": {"verification_status": "verified"},
                        "b": {"verification_status": "unverified"},
                        "c": {"verification_status": "judgment_unavailable"},
                        "d": {"verification_status": "conflicting"}}}]
    s = provenance_svc.summarize(rows)
    assert s["records"] == 1
    assert s["records_fully_verified"] == 0
    assert s["records_needing_review"] == 1
    assert s["fields_verified"] == 1
    assert s["fields_unverified"] == 1
    assert s["fields_judgment_unavailable"] == 1
    assert s["fields_conflicting"] == 1


def test_a_fully_verified_record_is_reported_as_such():
    rows = [{"fields": {"a": {"verification_status": "verified"},
                        "b": {"verification_status": "verified"}}}]
    s = provenance_svc.summarize(rows)
    assert s["records_fully_verified"] == 1
    assert s["records_needing_review"] == 0


def test_unjudged_never_counts_as_verified():
    rows = [{"fields": {"a": {"verification_status": "judgment_unavailable"}}}]
    s = provenance_svc.summarize(rows)
    assert s["fields_verified"] == 0
    assert s["fields_judgment_unavailable"] == 1


# --- the status is part of the contract -------------------------------------

def test_judgment_unavailable_is_a_valid_status():
    from app.schemas.plan import ProvenanceField
    f = ProvenanceField.model_validate(
        {"value": 1, "verification_status": "judgment_unavailable",
         "source": {"url": "u", "quote": "q", "retrieved_at": "t"}})
    assert f.verification_status == "judgment_unavailable"
