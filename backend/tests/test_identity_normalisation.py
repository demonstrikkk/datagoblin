"""Tests for identity normalisation of company names.

Context: a live run returned four records for one company --
`AWS`, `Amazon Web Services, Inc.`, `Amazon Web Services (AWS)` and `AWS`
again. `_identity` produced three distinct signatures, so dedupe could not
merge them and the dataset reported five records for roughly two real
entities.

The hard constraint, from this module's existing design: corporate form is
identity. `Acme Inc` and `Acme LLC` must stay apart, because merging them
unions one row's figures into the other with no conflict ever recorded. Every
test here is therefore paired with a "must stay distinct" twin.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.deduper import _acronym_of, _identity, _strip_parenthetical  # noqa: E402


# -- the case that prompted this -------------------------------------------------

def test_a_trailing_parenthetical_is_a_gloss_not_an_identity():
    assert _identity("Amazon Web Services (AWS)") == "amazon web services"


def test_the_live_run_still_yields_three_signatures_not_five_records():
    """What this change actually achieves, stated without overclaiming.

    Four records for one company produced three signatures before and three
    after. The parenthetical fix merges `Amazon Web Services (AWS)` into
    `Amazon Web Services`, which is real, but `aws` and
    `amazon web services inc` still stand alone -- so this does NOT yet fix the
    duplication the run exposed. Asserting the count here rather than the
    intent is deliberate: the residual is tracked by
    test_acronym_resolution_is_not_attempted_blindly and should not be quietly
    forgotten behind a test that implies the problem is solved.
    """
    names = ["AWS", "Amazon Web Services, Inc.", "Amazon Web Services (AWS)"]
    assert len({_identity(n) for n in names}) == 3


def test_the_parenthetical_variant_merges_with_the_plain_name():
    """The part that IS fixed."""
    assert _identity("Amazon Web Services (AWS)") == \
        _identity("Amazon Web Services")


def test_parenthetical_stripping_precedes_punctuation_removal():
    """Ordering bug this guards: normalising punctuation first erases the
    brackets, turning the name into `amazon web services aws`, which matches
    nothing."""
    assert _identity("Foo Corp (Beta)") == "foo corp"
    assert "aws" not in _identity("Amazon Web Services (AWS)")


# -- what must NOT merge ---------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Acme (Europe) Ltd", "Acme (US) Ltd"),      # middle parenthetical
    ("Acme Holdings", "Acme Holdings Ltd"),
    ("Northwind", "Northwind Trading"),
])
def test_distinguishable_entities_stay_distinct(a, b):
    assert _identity(a) != _identity(b), \
        f"merging {_identity(a)!r} with {_identity(b)!r} would conflate two entities"


def test_corporate_form_is_still_identity():
    """The rule this module was written around."""
    assert _identity("Acme Inc") != _identity("Acme LLC")


def test_an_empty_parenthetical_is_harmless():
    assert _identity("Acme ()") == "acme"


def test_a_name_that_is_only_a_parenthetical_does_not_vanish():
    assert _identity("(unknown)") == "unknown"


# -- helper behaviour ------------------------------------------------------------

def test_acronym_is_computed_only_for_multiword_names():
    assert _acronym_of("Amazon Web Services") == "aws"
    assert _acronym_of("AWS") == "", "a single word has no expansion to imply"
    assert _acronym_of("") == ""


def test_acronym_ignores_single_letters():
    """`S&P Global` must not expand to something that looks meaningful.

    Single letters are dropped before initials are taken, so `S&P Global`
    yields a one-word remainder and therefore no acronym at all. That is the
    conservative outcome: a wrong expansion would merge unrelated rows.
    """
    assert _acronym_of("S&P Global") == ""
    assert _acronym_of("Amazon Web Services") == "aws"


def test_acronym_of_an_entity_with_a_corporate_suffix():
    # `inc` is a word after normalisation, so this is 'awsi' rather than 'aws'.
    # That is why the bare acronym is not auto-merged: doing so would require
    # stripping corporate form first, which this module deliberately forbids.
    assert _acronym_of("Amazon Web Services, Inc.") == "awsi"


def test_acronym_resolution_is_not_attempted_blindly():
    """States the remaining gap, so it cannot be forgotten.

    Four records for one company became three signatures, then two. Collapsing
    `aws` into `amazon web services` needs corporate-form stripping, which would
    also collapse `Acme Inc` into `Acme LLC`. Until an unambiguous in-dataset
    alias resolution exists, this stays open rather than guessed.
    """
    assert _identity("AWS") == "aws"
    assert _identity("Amazon Web Services") == "amazon web services"
    assert _identity("AWS") != _identity("Amazon Web Services")


# -- robustness ------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "   ", 0, [], {}, "()", "(())"])
def test_hostile_input_does_not_raise(value):
    assert isinstance(_identity(value), str)


def test_a_name_that_is_only_punctuation_does_not_crash_the_strip():
    assert _identity("---") == ""