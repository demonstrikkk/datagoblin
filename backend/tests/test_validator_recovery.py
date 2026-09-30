"""The validation gate must not destroy values it cannot actually check.

Two rules that were fused, or that failed closed against a document the page
never had, silently blanked columns that the extractor had filled correctly.

Measured on a live dataset (123 records, 3 fields):

    financial_year   25 present / 98 null   of which 53 carried a quote
                                            and 53 carried a reference_id
    valuation        50 present / 73 null

Every one of those 53 `reference_id` failures came from an `http`-fetched URL,
while the identical page fetched by Crawl4AI produced none — because
`references` is only ever populated by the Crawl4AI rung, and the gate failed
closed on its absence. A plain-text table has no `⟨n⟩` citation spans, so the
marker the model emitted was invented, and the gate could not tell an invented
marker on an uncited page from a real marker missing from a References block.

The other 117 cells had no matching evidence item at all. `wrap_record` looks
the quote up by exact field-name match, so a value returned without its evidence
entry arrived with an empty quote and was nulled alongside the genuine
"quote is not on the page" case.

These tests pin both, and the distinction between them, because the fix is only
worth anything if the real failure still fails.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import validator as V  # noqa: E402

PAGE = "Paris is the capital of France. Founded 250 BC."


def grade(value, quote, page=PAGE, ftype="string", **kw):
    return asyncio.run(V.verify_field(value, quote, page, False, ftype, **kw))


# ------------------------------------------------------- reference_known gate


def test_no_reference_id_is_always_known() -> None:
    assert V.reference_known("", "") is True
    assert V.reference_known("", "## References\n1. Somewhere") is True


def test_marker_is_rejected_when_the_page_has_a_references_block() -> None:
    """The gate still bites when it can actually bite."""
    assert V.reference_known("7", "## References\n1. Somewhere") is False
    assert V.reference_known("1", "## References\n1. Somewhere") is True


@pytest.mark.parametrize("empty", ["", "   ", "\n", None])
def test_marker_on_a_page_with_no_references_block_is_not_a_failure(empty) -> None:
    """The bug.

    `http` and `jina` never set `references`, so this is the state of every
    page they fetch. Failing closed here nulled values whose quotes were
    verbatim and on the page.
    """
    assert V.reference_known("2", empty) is True


def test_marker_survives_when_the_page_has_no_citation_apparatus() -> None:
    """End to end through the gate, not just the helper."""
    value, status = grade("Paris", "Paris is the capital", reference_id="2", references="")
    assert value == "Paris"
    assert status in ("verified", "judgment_unavailable", "rate_limited", "unverified")


def test_unknown_marker_still_destroys_the_value_when_referenced() -> None:
    """The other direction: a real apparatus, a marker that is not in it."""
    value, status = grade("Paris", "Paris is the capital", reference_id="9",
                          references="## References\n1. Somewhere")
    assert value is None
    assert status == "unverified"


# -------------------------------------------------------- no-quote vs bad-quote


def test_value_with_no_quote_is_kept_as_unverified() -> None:
    """The bug, and the recovery.

    A model that returns a value but omits its evidence item is a
    prompt-following failure, not evidence that the source lacks the value.
    Nulling it claimed an absence the system had not established.
    """
    value, status = grade("Paris", "")
    assert value == "Paris"
    assert status == "unverified"


def test_value_with_no_quote_on_a_real_page_is_still_kept() -> None:
    """Not an artefact of the page being empty."""
    value, status = grade("Paris", "", page=PAGE)
    assert value == "Paris"
    assert status == "unverified"


def test_quote_that_is_not_on_the_page_still_destroys_the_value() -> None:
    """The distinction that must survive the fix.

    A quote that was supplied and cannot be located is a real failure — the
    value is hallucinated or attributed to the wrong sentence. This is not
    softened to a warning.
    """
    value, status = grade("Paris", "Completely different wording absent here")
    assert value is None
    assert status == "unverified"


def test_quote_present_and_locatable_still_verifies() -> None:
    """The happy path is untouched."""
    value, status = grade("Paris", "Paris is the capital")
    assert value == "Paris"
    assert status == "verified"


# ------------------------------------------------- ordering between the gates


def test_no_quote_is_decided_before_the_reference_gate() -> None:
    """A missing quote must not be reported as a citation failure.

    Both used to be one `return None` chain, so a value with no quote and a
    hallucinated marker were indistinguishable in the output. The no-quote case
    now returns the value, so the reference gate is never reached.
    """
    value, _ = grade("Paris", "", reference_id="2", references="")
    assert value == "Paris"


# --------------------------------------------------- the gates still gate things


def test_placeholder_is_still_rejected() -> None:
    """This gate is a deliberate design decision and must not be relaxed."""
    for junk in ("—", "N/A", "unknown", ""):
        value, status = grade(junk, PAGE)
        assert value is None, junk
        assert status == "unverified", junk


def test_type_mismatch_is_still_rejected() -> None:
    value, status = grade("not a number", PAGE, ftype="number")
    assert value is None
    assert status == "unverified"


def test_number_with_a_locatable_quote_survives() -> None:
    """Guards against the type gate being caught by a well-meaning change."""
    value, status = grade("250", "Founded 250 BC", ftype="number")
    assert value == 250
    assert status == "verified"
