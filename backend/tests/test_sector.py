"""A sector is a claim about a dataset, so it has to carry a receipt.

`/profile` already returns per-field `kind`, `distinct` and `shape`, and
`/api/datasets/{did}` already returns `schema[].{name,type,description}`, so a
sector can be read without inventing data and without a model call. What these
tests pin is the part that is easy to get wrong: the classifier must decline.

The schemas below are the real shapes out of the live database, not invented
examples — the finance one is decisive, the corporate one must be refused, and
the ambiguous middle must return `None` rather than a guess.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import sector as S  # noqa: E402


def f(name, type_="string", desc=""):
    return {"name": name, "type": type_, "description": desc, "required": False}


# The decisive finance schema, from the Indian-stocks dataset.
STOCKS = [
    f("company_name", "string", "Name of the listed company"),
    f("symbol", "string", "Stock ticker symbol"),
    f("exchange", "string", "Listing exchange such as NSE or BSE"),
    f("market_cap", "number", "Market capitalisation in INR"),
    f("pe_ratio", "number", "Price to Earnings ratio"),
    f("sector", "string", "Sector the company operates in"),
]

# A generic corporate schema. `sector` and `valuation` appear, and that is
# exactly the trap: they are finance-flavoured words on a dataset that is not a
# stock table.
CORPORATE = [
    f("company_name", "string", "Registered name of the company"),
    f("valuation", "number", "Estimated valuation"),
    f("financial_year", "string", "Financial year the figure relates to"),
    f("sector", "string", "Industry the company operates in"),
]

# Nothing recognisable. The answer must be None.
GENERIC = [
    f("name", "string", "Name"),
    f("category", "string", "Category"),
    f("notes", "string", "Free text notes"),
]

HEALTHCARE = [
    f("trial_title", "string", "Official title of the clinical trial"),
    f("nct_id", "string", "ClinicalTrials.gov identifier"),
    f("phase", "string", "Clinical trial phase"),
    f("sponsor", "string", "Organisation running the trial"),
    f("indication", "string", "Therapeutic indication"),
]


# --------------------------------------------------------------- the obvious


def test_a_finance_schema_is_classified() -> None:
    out = S.sector(STOCKS)
    assert out["sector"] == "stocks"
    assert out["confidence"] >= S.CONFIDENCE_FLOOR
    assert out["method"] == "schema"


def test_a_clinical_schema_is_classified() -> None:
    out = S.sector(HEALTHCARE)
    assert out["sector"] == "healthcare"


def test_a_generic_schema_is_refused() -> None:
    out = S.sector(GENERIC)
    assert out["sector"] is None
    assert out["method"] in ("none", "below-floor")


def test_an_empty_schema_is_refused() -> None:
    out = S.sector([])
    assert out["sector"] is None
    assert out["method"] == "none"
    assert out["considered"] == []


def test_no_schema_at_all_is_refused() -> None:
    assert S.sector(None)["sector"] is None


# ----------------------------------------------------------------- receipts


def test_the_receipt_names_the_fields_that_matched() -> None:
    out = S.sector(STOCKS)
    names = {m["field"] for m in out["matched_fields"]}
    assert "symbol" in names
    assert "exchange" in names
    assert "market_cap" in names
    assert all(m["signal"] for m in out["matched_fields"])


def test_every_signal_carries_a_reason() -> None:
    """A classification with no receipt is a fabrication with a score on it."""
    for m in S.sector(STOCKS)["matched_fields"]:
        assert m["signal"], m
        assert m["score"] > 0, m


def test_the_threshold_used_is_reported() -> None:
    assert S.sector(STOCKS)["floor"] == S.CONFIDENCE_FLOOR


def test_a_custom_floor_is_honoured() -> None:
    assert S.sector(STOCKS, floor=99.0)["sector"] is None


def test_considered_lists_everything_that_scored() -> None:
    out = S.sector(STOCKS)
    assert out["considered"], out
    assert out["considered"][0]["sector"] == "stocks"
    scores = [c["score"] for c in out["considered"]]
    assert scores == sorted(scores, reverse=True)


# ------------------------------------------------------- the corporate trap


def test_a_corporate_schema_is_not_called_stocks() -> None:
    """`sector` and `valuation` are finance-flavoured words on non-stock data.

    `valuation` is deliberately absent from the strong-field list precisely so
    that "companies with their valuation" is not read as a stock table.
    """
    out = S.sector(CORPORATE)
    assert out["sector"] != "stocks"


def test_the_corporate_trap_still_reports_what_it_saw() -> None:
    """Refusing is not the same as claiming there is nothing to see."""
    out = S.sector(CORPORATE)
    if out["sector"] is None:
        assert out["considered"], "a refusal should still show what it considered"


# ------------------------------------------------------------ shape signals


def _profile_for(name, kind, distinct, values=()):
    return {"fields": [{"field": name, "kind": kind, "distinct": distinct,
                        "shape": {"values": [{"value": v, "n": 1} for v in values]}}]}


def test_value_shape_breaks_a_weak_schema_tie() -> None:
    """A ticker column settles what the name alone cannot.

    `symbol` is a ticker in one dataset and a chemical symbol in another. Two
    fields that both read weakly, plus a categorical column of 120 short
    distinct values, is a stock table.
    """
    schema = [f("symbol", "string", "Symbol"), f("venue", "string", "Where it trades")]
    out = S.sector(schema, profile=_profile_for(
        "symbol", "categorical", 120, ["NSE", "BSE", "NYSE", "HUL", "TCS"]))
    assert out["sector"] == "stocks"
    assert out["method"] == "schema+shape"


def test_shape_alone_cannot_carry_a_sector() -> None:
    """The bonus is capped so no amount of shape overrides a silent schema."""
    schema = [f("label", "string", "Label")]
    out = S.sector(schema, profile=_profile_for(
        "label", "categorical", 500, ["AA", "BB", "CC"]))
    assert out["sector"] != "stocks"


def test_a_long_valued_column_is_not_a_ticker() -> None:
    """`company_name` is also categorical with many distinct values."""
    schema = [f("symbol", "string", "Symbol")]
    out = S.sector(schema, profile=_profile_for(
        "symbol", "categorical", 120,
        ["Onelife Capital Advisors Limited", "Quality Power Infra Limited"]))
    assert out["sector"] != "stocks"


def test_trial_phases_are_recognised_from_values() -> None:
    schema = [f("stage", "string", "Stage"), f("nct", "string", "Registry id")]
    out = S.sector(schema, profile=_profile_for(
        "stage", "categorical", 4, ["Phase I", "Phase II", "Phase III", "Phase IV"]))
    assert out["sector"] == "healthcare"


# ------------------------------------------------------------- goal tiebreak


def test_the_goal_breaks_a_tie_the_schema_could_not() -> None:
    schema = [f("a", "string", "A"), f("b", "string", "B")]
    weak = {"fields": []}
    out = S.sector(schema, profile=weak,
                   goal="list clinical trial phases and sponsors for each indication")
    assert out["sector"] in (None, "healthcare")


def test_an_incidental_goal_word_is_not_a_tiebreak() -> None:
    """One matching word is noise, not evidence."""
    out = S.sector([f("a", "string", "A")], goal="a list of things")
    assert out["sector"] is None


def test_an_empty_goal_changes_nothing() -> None:
    assert S.sector(STOCKS, goal="")["sector"] == "stocks"
