"""A quote from a rendered page must resolve even though the page kept its
markdown emphasis markers.

Found by running a real investigation, not by reading the code. A live run asked
for logistics companies with their CEOs and HQ cities. Two plain-HTTP pages
produced 139 records; the one page delivered by Chromium
(`supplychaindigital.com/top10/top-10-logistics-ceos`, 30 kB, 78 mentions of
"CEO") produced **zero**:

    record.extracted  'Extracted 13 from .../top-10-logistics-ceos (opencode)'
    record.rejected   'required field(s) not evidenced: company_name'   x13

The stored text held `**Company:** C.H. Robinson`. The model quoted
`Company: C.H. Robinson`. The exact, case-insensitive and whitespace-collapsed
passes all missed it, the required field was ruled unevidenced, and the whole
record was dropped -- 13 of them. The difference between the pages that worked
and the page that did not was the emphasis markers, not the content.

Three separate checks had to agree and only two of them were ever going to:

  * `validator.verify_field` gates the quote against the source,
  * `locate_quote` turns it into offsets,
  * `jev.deterministic_verdict` re-checks the quote before judging.

All three now read `evidence.normalize_for_match`. The tests below fail on the
pre-change tree at the gate and the judge; the locator tests fail there too.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.evidence import (  # noqa: E402
    locate_quote, normalize_for_match, normalize_with_map)

# The exact shape the live page stored, copied from the reduced text.
PAGE = ("## 10. Dave Bozeman\n"
        "**Company:** C.H. Robinson **Revenue:** US$17.7bn **Employees:** 14,000+\n"
        "Dave Bozeman is CEO of the logistics firm.")


def _folded(s: str) -> str:
    """Whitespace-and-markdown-insensitive comparison for assertions."""
    return re.sub(r"[*_`\s]+", " ", str(s or "")).strip().lower()


# --- the locator -------------------------------------------------------------

def test_quote_without_markers_is_located_in_marked_text():
    a, b = locate_quote("Company: C.H. Robinson", PAGE)
    assert a is not None and b is not None, (
        "the model's de-emphasised quote must locate in the stored markdown")
    assert "C.H. Robinson" in PAGE[a:b]


def test_located_slice_contains_the_markers_the_quote_dropped():
    """Documented consequence: the span includes `**`, which is why `settle`
    stores the slice as the quote rather than the model's spelling."""
    a, b = locate_quote("Company: C.H. Robinson", PAGE)
    assert "**" in PAGE[a:b], (
        "the located span covers the original characters, markers included")


def test_bare_value_is_located_exactly():
    a, b = locate_quote("C.H. Robinson", PAGE)
    assert PAGE[a:b] == "C.H. Robinson"


def test_backticks_and_underscores_fold_too():
    assert locate_quote("code sample", "**`code`** __sample__")[0] is not None
    # An underscore used as emphasis sits between words; folding it must join
    # them, not leave a hole where the marker was.
    assert locate_quote("a b c", "a _b_ c")[0] is not None


def test_exact_match_still_wins_over_the_new_pass():
    src = "Acme zz **Acme** yy"
    a, b = locate_quote("Acme", src)
    assert (a, b) == (0, 4), "the first literal occurrence is the exact match"


def test_absent_quote_is_still_none():
    assert locate_quote("Globex", PAGE) == (None, None)


def test_a_quote_spanning_a_markdown_link_is_refused_not_mislocated():
    """Deliberate limitation, stated rather than hidden.

    The link's own text still locates (it is a literal substring). What is NOT
    rescued is a quote that spans the delimiters: mapping it back across the URL
    is ambiguous, and a wrong offset is worse than a refused one.
    """
    src = "By [Louise Collins](https://example.com/a/long/path) today"
    # The link text alone is present verbatim, so it locates without folding.
    assert locate_quote("Louise Collins", src)[0] is not None
    # Spanning the delimiters is refused, loudly.
    assert locate_quote("By Louise Collins today", src) == (None, None)


def test_the_fold_cannot_invent_a_match_between_unrelated_strings():
    assert locate_quote("qqq zzz", PAGE) == (None, None)


def test_normalize_for_match_is_symmetric():
    assert normalize_for_match("**Company:** C.H. Robinson") == \
        normalize_for_match("Company: C.H. Robinson") == \
        normalize_for_match("__company__: c.h. robinson")


def test_normalize_with_map_points_only_at_original_characters():
    text = "**a** b`c`"
    norm, imap = normalize_with_map(text)
    assert norm == "a bc"
    assert len(norm) == len(imap)
    for k, i in enumerate(imap):
        assert text[i] == norm[k], (
            f"index {k} maps to original {i} ({text[i]!r}) but reports "
            f"{norm[k]!r}")


# --- the three checks that must agree ---------------------------------------

def _gate_admits(quote: str, src: str) -> bool:
    """Replicates `validator.verify_field`'s admission test exactly."""
    from app.services import validator as validator_svc
    return validator_svc.normalize_for_match(quote) in \
        validator_svc.normalize_for_match(src)


def test_the_gate_admits_the_de_emphasised_quote():
    assert _gate_admits("Company: C.H. Robinson", PAGE), (
        "pre-fix the gate rejected this and every record was dropped")


def test_the_judge_pre_check_does_not_null_the_admitted_quote():
    """The failure that would have survived a gate-only fix.

    `verify_field` admits the quote, then calls `deterministic_verdict`, which
    used its own whitespace-only fold, looked for a quote the gate had just
    accepted, did not find it, and returned NOT_SUPPORTED. The value was nulled
    one line after being admitted.
    """
    from app.providers.decision import jev
    verdict = jev.deterministic_verdict(
        "C.H. Robinson", "Company: C.H. Robinson", PAGE)
    assert verdict is not None, (
        "a value inside its own quote must be settled deterministically")
    assert verdict["judgment"] == "SUPPORTED", (
        f"got {verdict['judgment']!r}; the quote is on the page")


def test_verify_field_keeps_the_value_instead_of_nulling_it():
    """The whole record-level outcome: `company_name` survives validation.

    Pre-fix this returns `(None, "unverified")`, which the runner reports as
    "required field(s) not evidenced: company_name" and drops the record.
    """
    from app.services import validator as validator_svc
    value, status = asyncio.run(validator_svc.verify_field(
        "C.H. Robinson", "Company: C.H. Robinson", PAGE,
        required=True, ftype="string", judged_by_construction=True))
    assert value == "C.H. Robinson", (
        "the value must not be nulled: it is on the page, just marked up")
    assert status == "verified"


# --- the invariant the store depends on -------------------------------------

def test_settle_stores_a_quote_that_is_exactly_the_stored_text():
    """`quote == source[start:end]`, for every verified cell.

    Checked against 299 of 299 verified cells in a live dataset before this
    change; the markdown pass could have broken it, so it is pinned here.
    """
    from app.services import validator as validator_svc
    cell = {"value": "C.H. Robinson",
            "source": {"url": "u", "quote": "Company: C.H. Robinson"}}
    out = validator_svc.settle(cell, status="verified",
                               quote="Company: C.H. Robinson",
                               source_text=PAGE)
    assert out["verification_status"] == "verified"
    src = out["source"]
    assert PAGE[src["start"]:src["end"]] == src["quote"], (
        "a reader must be able to slice the page and get the quote shown")
    assert "C.H. Robinson" in src["quote"]


def test_settle_leaves_an_exact_match_unchanged():
    from app.services import validator as validator_svc
    cell = {"value": "C.H. Robinson",
            "source": {"url": "u", "quote": "C.H. Robinson"}}
    out = validator_svc.settle(cell, status="verified",
                               quote="C.H. Robinson", source_text=PAGE)
    assert out["source"]["quote"] == "C.H. Robinson"


def test_settle_still_demotes_an_unlocatable_quote():
    """The markdown pass must not turn `settle` into a rubber stamp."""
    from app.services import validator as validator_svc
    cell = {"value": "Globex", "source": {"url": "u", "quote": "Globex"}}
    out = validator_svc.settle(cell, status="verified", quote="Globex",
                               source_text=PAGE)
    assert out["verification_status"] == "unverified"
    assert out["source"]["start"] is None
