"""The receipt must agree with the gate, in the direction that flatters nobody.

`field_confidence`'s own docstring is the contract:

    The verified gate is absolute, not a discount. `field_confidence` returns
    the prior or exactly `0`. A value whose evidence could not be checked scores
    nothing, not "something less than nothing".

Two call sites ignored that and substituted the extraction route:

  * `extractor._ladder_cells` computed `verified = method in {api, json_ld,
    labelled}` and stamped the receipt *before the gate had run at all*.
  * `validator._receipt_for` then preserved that stamped receipt verbatim, so
    the gate's refusal had no way to reach it — and where there was no receipt
    it recomputed the same verdict as
    `status == "verified" or method in {api, json_ld, labelled}`.

The result was a row that contradicted itself. A cell whose quote was not
anywhere on the stored page came back

    verification_status: "unverified"      <- the gate
    confidence:          0.99              <- the route
    certainty:           "sure"            <- the UI badge the reader sees

`api` carries the highest prior in `METHOD_PRIOR` precisely because it is the
route that should be least surprised by a missing quote — so the routes that
most needed the gate were the ones that overrode it. Nothing errored. The
number a reader relies on disagreed with the sentence beside it, and both were
persisted in the same record.

The converse matters as much: a structured route that *passes* its gates must
still be confident. That promotion happens inside `verify_field`, after the
quote and type checks, so this is not a demotion of the ladder — it is the
ladder being allowed to mean what it says.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import confidence as conf_svc  # noqa: E402
from app.services import extractor as extractor_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


PAGE = "Acme Corp was founded in 2011 and employs roughly 240 people."

FIELDS = [{"name": "headcount", "type": "string", "required": False}]


def _raw(value, quote, route="api", claim_verified=True):
    """A ladder cell as it arrives at the gate, claiming it was verified.

    `claim_verified=True` is deliberate and is the point of several tests below.
    It reproduces what `_ladder_cells` really did emit for a structured route --
    a receipt computed from the route name, before anything had been checked.
    The chokepoint's job is to override an upstream claim that the gate has just
    rejected, so the test has to feed it one.

    A helper that quietly corrected the claim would pass against the defective
    `_receipt_for`, which is exactly what the first version of this file did.
    """
    return {
        "fields": {"headcount": {
            "value": value,
            "verification_status": "verified" if claim_verified else "unverified",
            "source": {"url": "https://s.example/a", "title": "t",
                       "quote": quote, "retrieved_at": "now"},
            **conf_svc.receipt(route, verified=claim_verified),
        }},
        "evidence": [{"field": "headcount", "value": value, "quote": quote,
                      "read_method": route}],
    }


def _wrap(raw, source_text=PAGE):
    return run(validator_svc.wrap_record(
        FIELDS, raw, source_text, "https://s.example/a", "Acme", "now",
        budget=validator_svc.JudgeBudget(0, unlimited=True)))


def test_the_upstream_claim_is_overridden_when_the_gate_refuses():
    """The chokepoint, isolated.

    Everything upstream claims `verified` for a structured route. The gate is
    handed a value whose quote is not on the page and refuses it. The persisted
    cell must carry the gate's answer, not the one it arrived with.
    """
    raw = _raw("acme-global", "acme-global", route="api", claim_verified=True)
    assert raw["fields"]["headcount"]["confidence"] == 0.99, (
        "the fixture must actually carry a confident upstream claim, or this "
        "test proves nothing")

    cell = _wrap(raw)["headcount"]
    assert cell["verification_status"] != "verified"
    assert cell["confidence"] == 0.0


# --- the defect -------------------------------------------------------------

def test_a_refused_cell_does_not_keep_a_confident_receipt():
    """The load-bearing assertion: status and confidence cannot disagree."""
    # "acme-global" appears nowhere on the page, so the quote cannot support it.
    cell = _wrap(_raw("acme-global", "acme-global"))["headcount"]

    assert cell["verification_status"] != "verified"
    assert cell["confidence"] == 0.0, (
        "the gate refused this value but the receipt still reported "
        f"{cell['confidence']}, on a route whose prior is 0.99")
    assert cell["certainty"] != "sure", (
        "a value nobody checked must not render as 'sure' to the reader")
    assert cell["certainty"] == "check"


def test_a_missing_quote_is_never_confident_on_any_route():
    """Every route, not just the one that was reported."""
    for route in sorted(conf_svc.METHOD_PRIOR):
        cell = _wrap(_raw("acme-global", "acme-global", route=route))["headcount"]
        assert cell["confidence"] == 0.0 or cell["verification_status"] == "verified", (
            f"route {route!r}: status {cell['verification_status']!r} but "
            f"confidence {cell['confidence']}")
        assert cell["certainty"] != "sure" or cell["verification_status"] == "verified", (
            f"route {route!r} renders as 'sure' without being verified")


def test_the_two_fields_never_disagree():
    """Exhaustive over routes, because a per-route spot check is how this hid.

    This is the invariant the row has to satisfy, stated once: the badge a
    reader sees is derived from the number, and both are derived from the same
    gate answer. If `verification_status` is not `verified`, nothing may claim
    otherwise.
    """
    for route in sorted(conf_svc.METHOD_PRIOR):
        for value, quote in (("acme-global", "acme-global"),   # not on the page
                             ("240", "employs 999 people")):   # right value,
                                                               # quote not on page
            cell = _wrap(_raw(value, quote, route=route)).get("headcount")
            if cell is None:
                continue
            if cell["verification_status"] != "verified":
                assert cell["confidence"] == 0.0, (
                    f"{route}: {cell['verification_status']} carried "
                    f"confidence {cell['confidence']}")
                assert cell["certainty"] == "check", (
                    f"{route}: {cell['verification_status']} carried "
                    f"certainty {cell['certainty']!r}")


# --- nothing legitimate is lost ---------------------------------------------

def test_a_structured_route_that_passes_its_gates_is_still_confident():
    """The ladder must not be demoted by the fix.

    `api` has the highest prior precisely because a feed states the field
    outright. When the quote really is on the page, `verify_field` promotes it
    by construction -- after the quote and type checks -- and the receipt should
    say so.
    """
    cell = _wrap(_raw("240", "employs roughly 240 people", route="api"))["headcount"]
    assert cell["verification_status"] == "verified"
    assert cell["read_method"] == "api", "the route must still be recorded"
    assert cell["confidence"] == conf_svc.METHOD_PRIOR["api"] == 0.99
    assert cell["certainty"] == "sure"


def test_the_ladder_no_longer_claims_verification_before_the_gate_runs():
    """`_ladder_cells` runs before any check, so it may not assert one.

    Its output is persisted through `wrap_record`, which re-decides per cell --
    but anything reading a raw extraction believed the stamp.
    """
    cells = extractor_svc._ladder_cells(
        {"company_name": "Acme Corp"}, {"company_name": "Acme Corp"},
        {"company_name": "api"}, "https://s.example/a", "t", "now")

    cell = cells["company_name"]
    assert cell["verification_status"] != "verified", (
        "the extractor asserts this before the gate has looked at anything")
    assert cell["confidence"] == 0.0
    assert cell["certainty"] == "check"


@pytest.mark.parametrize("method,expected", [
    ("api", 0.99), ("json_ld", 0.95), ("labelled", 0.90), ("llm", 0.75),
])
def test_confidence_is_the_prior_when_verified_and_zero_otherwise(method, expected):
    """The contract in `field_confidence`, asserted directly."""
    assert conf_svc.field_confidence(method, True) == expected
    assert conf_svc.field_confidence(method, False) == 0.0