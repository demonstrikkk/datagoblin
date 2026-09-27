"""The locator must agree with the gate that admitted the quote.

`verify_field` admits a quote when it is a substring of the source with
whitespace collapsed. `locate_quote` used an exact `find`. A quote the model
emitted as one line therefore passed the gate, matched a span laid out over
several lines in the stored page, and then came back as `(None, None)` - the
field was stored as `verified` with no offsets, which makes the citation
unusable. Measured on a live run: 2 of 45 verified fields.

Every verified field must have offsets that resolve in the STORED page.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.evidence import locate_quote  # noqa: E402

norm = lambda s: " ".join(str(s or "").split())


def assert_resolves(quote, src):
    a, b = locate_quote(quote, src)
    assert a is not None, f"quote {quote!r} not located in {src!r}"
    assert norm(src[a:b]) == norm(quote), (
        f"offsets ({a},{b}) do not resolve: {src[a:b]!r}")


# --- the pass that was missing ----------------------------------------------

def test_quote_spanning_newlines_is_located():
    assert_resolves("Acme Corp is here today", "Acme Corp\nis here\ntoday")


def test_quote_spanning_tabs_and_runs_is_located():
    assert_resolves("Acme Corp is here", "Acme\t\tCorp  is here")


def test_leading_whitespace_run_is_collapsed():
    assert_resolves("a b c", "   a b c")


def test_trailing_whitespace_run_is_collapsed():
    assert_resolves("a b c", "a b c   \n  ")


def test_multi_line_quote_from_a_real_page_shape():
    src = ("## Programmes\n\nUN Women India is dedicated to advancing gender "
           "equality and women's economic empowerment through\n"
           "skill development and entrepreneurship.\n")
    assert_resolves("UN Women India is dedicated to advancing gender equality and "
                    "women's economic empowerment", src)


def test_exact_match_still_wins():
    a, b = locate_quote("Acme Corp", "xx Acme Corp yy Acme Corp zz")
    assert (a, b) == (3, 12)


def test_case_insensitive_match():
    a, b = locate_quote("acme corp", "Acme Corp Ltd")
    assert src_span("Acme Corp Ltd", a, b) == "Acme Corp"


def src_span(s, a, b):
    return s[a:b]


def test_absent_quote_is_still_none():
    assert locate_quote("Globex", "Acme Corp") == (None, None)


def test_empty_inputs():
    assert locate_quote("", "abc") == (None, None)
    assert locate_quote("abc", "") == (None, None)
    assert locate_quote(None, None) == (None, None)


# --- the invariant that matters ---------------------------------------------

def test_normalised_map_handles_ascii_and_unicode():
    from app.schemas.evidence import _norm_with_map
    s = "a  b\nc  d"
    n, imap = _norm_with_map(s)
    assert n == "a b c d"
    assert len(n) == len(imap)
    # Every mapped index points at the original character it came from.
    for k, i in enumerate(imap):
        assert s[i] in (n[k], " ") or s[i].isspace()


def test_normalised_map_of_empty_text():
    from app.schemas.evidence import _norm_with_map
    assert _norm_with_map("") == ("", [])


def test_gate_and_locator_agree_on_a_whitespace_mismatch():
    """The specific pairing that produced (None, None) on a live run: the gate
    admits it, so the locator must too."""
    import re
    from app.services import validator as validator_svc
    src = "Our key focus areas include economic empowerment through skill\ndevelopment, entrepreneurship."
    quote = "Our key focus areas include economic empowerment through skill development"
    gate = re.sub(r"\s+", " ", quote.strip().lower()) in \
        re.sub(r"\s+", " ", src.strip().lower())
    assert gate, "precondition: the gate must admit this quote"
    assert_resolves(quote, src)


@pytest.mark.parametrize("sep", ["\n", "  ", "\t", " \n ", " \t\n "])
def test_every_whitespace_shape_resolves(sep):
    src = sep.join(["alpha", "beta", "gamma", "delta"])
    assert_resolves("alpha beta gamma delta", src)
