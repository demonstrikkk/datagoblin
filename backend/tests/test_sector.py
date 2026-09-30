"""A sector is a claim about a dataset, so it has to carry a receipt.

The vocabulary in `app.services.sector` was written from imagination the first
time and matched almost nothing: the live workspace returned
`{"sector": null, "method": "none"}` for a dataset about AI startups, because
every token belonged to a domain no stored dataset contained. Silent, and it
looked like the panel simply not loading.

So the test corpus is the corpus. `tests/fixtures/schema_corpus.json` is 20 real
datasets — 197 schema fields with the descriptions the planner actually wrote —
frozen from the live API. This module runs the scorer over all of them and asserts
what it does with each. When the planner invents a new field name, this is what
notices, which a handful of hand-written examples never would.

The expected answers are argued, not discovered. Where the honest answer is
"refused", the test says refused and explains why, because a refusal on a mixed
schema is a feature and a bug if it silently became a guess.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import sector as S  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "schema_corpus.json")


def _load() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["datasets"]


CORPUS = _load()


def classify(entry: dict) -> dict:
    return S.sector(entry["fields"], goal=entry["label"])


def by_prefix(prefix: str) -> list[dict]:
    return [e for e in CORPUS if e["label"].lower().startswith(prefix.lower())]


# ---------------------------------------------------------------- the corpus


def test_the_corpus_is_the_real_thing() -> None:
    """If the fixture is ever gutted, every assertion below becomes vacuous."""
    assert len(CORPUS) == 20, "the frozen corpus should hold all 20 datasets"
    fields = {f["name"] for e in CORPUS for f in e["fields"]}
    assert len(fields) >= 110, f"expected the full field vocabulary, got {len(fields)}"
    # Spot-check that it is the live corpus and not a placeholder.
    for name in ("yc_slug", "pe_ratio", "ngo_name", "total_funding_usd",
                 "beneficiaries_reached", "open_engineering_roles_count"):
        assert name in fields, name


# ------------------------------------------------------------------ markets


def test_the_indian_stocks_dataset_is_markets() -> None:
    entries = by_prefix("Identify the top 10 stocks")
    assert entries, "the fixture should hold the Indian-stocks dataset"
    for e in entries:
        out = classify(e)
        assert out["sector"] == "markets", f"{e['label']}: {out}"
        assert out["confidence"] > 8, out


def test_markets_is_justified_by_the_fields_that_mean_it() -> None:
    out = classify(by_prefix("Identify the top 10 stocks")[0])
    names = {m["field"] for m in out["matched_fields"]}
    for decisive in ("market_cap", "pe_ratio", "isin", "exchange", "last_price"):
        assert decisive in names, f"{decisive} should have argued for markets: {sorted(names)}"


# ------------------------------------------------------------------ venture


def test_the_yc_directory_dataset_is_venture() -> None:
    """The largest schema in the workspace, and the clearest signal in it."""
    entries = by_prefix("Enumerate up to 50 companies")
    assert entries, "the fixture should hold the YC directory dataset"
    for e in entries:
        out = classify(e)
        assert out["sector"] == "venture", f"{e['label']}: {out}"


def test_venture_is_justified_by_funding_and_batch_fields() -> None:
    out = classify(by_prefix("Enumerate up to 50 companies")[0])
    names = {m["field"] for m in out["matched_fields"]}
    for decisive in ("yc_slug", "batch_season", "total_funding_usd",
                     "latest_round_type", "founder_names"):
        assert decisive in names, f"{decisive}: {sorted(names)}"


def test_every_startup_dataset_is_venture() -> None:
    """The startup family is the most common request in the workspace."""
    entries = (by_prefix("Find AI startups") + by_prefix("Find 15 AI startups")
               + by_prefix("Identify top 5 Indian AI startups"))
    assert len(entries) >= 4, entries
    for e in entries:
        out = classify(e)
        assert out["sector"] == "venture", f"{e['label']}: {out}"


def test_a_thin_startup_schema_still_reads() -> None:
    """The dataset that returned `method: none` before the vocabulary rebuild.

    Four fields only: `startup_name`, `founders`, `latest_funding`,
    `active_engineering_roles`. That is the regression this whole change exists to
    fix, so it is asserted by name rather than left to a family test.
    """
    out = classify(by_prefix("Find AI startups in London")[0])
    assert out["sector"] == "venture", out
    names = {m["field"] for m in out["matched_fields"]}
    assert "latest_funding" in names
    assert "founders" in names


def test_a_five_field_series_a_schema_is_refused() -> None:
    """A thin schema is refused even when its one decisive field is genuine.

    `company_name, website, funding_round, industry, city` — five fields, one of
    which (`funding_round`) is a strong venture signal. It still scores 1.0,
    under the floor, and that is the right outcome rather than a disappointing
    one: rendering Venture panels would put a funding chart and an investor panel
    in front of a dataset that has no funding and no investor fields. The goal
    says "Series A", and a goal is a caption on the schema, not the schema.

    Refusing here is the same rule as the profiler's: do not show a shape the data
    cannot support.
    """
    entries = by_prefix("Find 3 Series A")
    assert entries, "the fixture should hold the Series A dataset"
    for e in entries:
        out = classify(e)
        assert out["sector"] is None, f"{e['label']} should be refused: {out}"
        assert out["method"] == "below-floor", out
        # It still has to report what it saw, or a refusal explains nothing.
        assert out["considered"], out
        assert out["considered"][0]["sector"] == "venture", out
        assert out["matched_fields"], out


# ----------------------------------------------------------------- nonprofit


def test_every_delhi_ngo_dataset_is_nonprofit() -> None:
    entries = (by_prefix("Identify active NGOs") + by_prefix("Find 5 active NGOs"))
    assert len(entries) >= 4, entries
    for e in entries:
        out = classify(e)
        assert out["sector"] == "nonprofit", f"{e['label']}: {out}"


def test_nonprofit_is_justified_by_registration_fields() -> None:
    out = classify(by_prefix("Identify active NGOs operating in Delhi")[0])
    names = {m["field"] for m in out["matched_fields"]}
    for decisive in ("ngo_name", "registration_id", "legal_status",
                     "beneficiaries_reached"):
        assert decisive in names, f"{decisive}: {sorted(names)}"


def test_womens_empowerment_programs_do_not_pull_it_elsewhere() -> None:
    """Program themes are the subject, not the domain.

    A dataset about women's empowerment carries `funding_sources` and
    `funding_partners`, which are venture vocabulary. On their own they must not
    reach the floor, or every grant-funded programme directory reads as a
    startup dataset.
    """
    out = classify(by_prefix("Find 5 active NGOs in Delhi")[0])
    assert out["sector"] == "nonprofit", out
    assert out["confidence"] >= S.CONFIDENCE_FLOOR


# --------------------------------------------------- the ones that must refuse


def test_eu_revenue_filings_are_not_a_stock_table() -> None:
    """`is_public_company` means "files periodic reports", not "is listed".

    The dataset is "AI companies that publish annual revenue" — a filings
    lookup. It has revenue, fiscal year, a source URL and a public-company flag,
    and no exchange, no price, no market cap, no P/E. Reading it as `markets`
    would be confidently wrong, so it is refused.
    """
    entries = by_prefix("List AI companies")
    assert entries, "the fixture should hold the EU revenue datasets"
    for e in entries:
        out = classify(e)
        assert out["sector"] != "markets", f"{e['label']} must not read as markets: {out}"


def test_collaboration_vendors_are_refused_as_a_tie() -> None:
    """A ticker column is not a stock table.

    The dataset is about collaboration-software vendors. It carries
    `ticker_symbol` and `is_public` alongside `funding_stage` and
    `employee_count`, so markets, venture and workforce all score and none wins.
    Refusing is correct; picking the highest would have picked one at random.
    """
    entries = by_prefix("Discover companies operating in the collaboration")
    assert entries, "the fixture should hold the collaboration datasets"
    for e in entries:
        out = classify(e)
        assert out["sector"] is None, f"{e['label']} should be refused: {out}"
        assert out["method"] in ("ambiguous", "below-floor"), out
        assert out["considered"], "a refusal must still show what it considered"


def test_a_stub_schema_with_no_domain_signal_is_refused() -> None:
    """Two fields, neither of which says what the records are."""
    for e in by_prefix("find top 10 stocks in india share market as of today"):
        out = classify(e)
        assert out["sector"] is None, f"{e['label']}: {out}"


def test_the_whole_corpus_is_classified_or_refused_deliberately() -> None:
    """Nothing may be left unexplained.

    Every dataset in the corpus must come back either with a sector and a
    confidence above the floor, or with a `method` saying why not. A silent
    empty result is the failure this file exists to catch.
    """
    for e in CORPUS:
        out = classify(e)
        if out["sector"] is None:
            assert out["method"] in ("none", "ambiguous", "below-floor"), \
                f"{e['label']}: {out}"
        else:
            assert out["confidence"] >= out["floor"], f"{e['label']}: {out}"
            assert out["matched_fields"], f"{e['label']} returned a sector with no receipt"
            assert all(m["signal"] for m in out["matched_fields"]), out


# ---------------------------------------------------------------- the receipt


def test_a_refusal_still_reports_what_it_saw() -> None:
    out = classify(by_prefix("Discover companies operating in the collaboration")[0])
    assert out["sector"] is None
    assert out["considered"], "a refusal with nothing considered is not an explanation"
    assert out["floor"] == S.CONFIDENCE_FLOOR
    assert out["tie_margin"] == S.TIE_MARGIN


def test_the_floor_used_is_reported_and_honoured() -> None:
    out = S.sector(by_prefix("Enumerate up to 50 companies")[0]["fields"])
    assert out["floor"] == S.CONFIDENCE_FLOOR
    assert S.sector(by_prefix("Enumerate up to 50 companies")[0]["fields"],
                    floor=999.0)["sector"] is None


def test_considered_is_ordered_and_non_empty_for_a_real_reading() -> None:
    out = classify(by_prefix("Enumerate up to 50 companies")[0])
    assert out["considered"]
    scores = [c["score"] for c in out["considered"]]
    assert scores == sorted(scores, reverse=True)


def test_an_empty_schema_is_refused() -> None:
    assert S.sector([])["sector"] is None
    assert S.sector(None)["sector"] is None


# ------------------------------------------------------------- vocabulary meta


def test_the_attested_set_is_actually_attested() -> None:
    """A domain claimed as attested must win somewhere in the corpus.

    Otherwise the documentation claims coverage the data does not support, which
    is the same mistake in the opposite direction.
    """
    seen = {classify(e)["sector"] for e in CORPUS}
    for name in S.ATTESTED:
        assert name in seen, f"{name} is documented as attested but no dataset reads as it"


def test_supported_domains_fire_but_are_not_about_them() -> None:
    """`workforce` is real vocabulary that this corpus has no clean instance of.

    It scores on `open_roles` and `team_size` across the startup datasets, and
    the venture reading is always stronger — correctly, because those datasets are
    about companies, not about headcount. So it must fire in `considered` and
    must never win.
    """
    fired = False
    for e in CORPUS:
        out = classify(e)
        assert out["sector"] not in S.SUPPORTED, f"{e['label']} matched {out['sector']}"
        if any(c["sector"] in S.SUPPORTED for c in out["considered"]):
            fired = True
    assert fired, "workforce vocabulary should fire on the startup datasets' hiring fields"


def test_no_unattested_domain_can_fire_on_the_corpus() -> None:
    """The unproven entries must stay unproven.

    `realestate`, `healthcare`, `education` and `energy` are in the vocabulary so
    a dataset in one of those domains does not need the file rewritten first. If
    any of them can match this corpus they are not unproven, they are wrong.
    """
    for e in CORPUS:
        out = classify(e)
        assert out["sector"] not in S.UNATTESTED, f"{e['label']} matched {out['sector']}"
        for c in out["considered"]:
            assert c["sector"] not in S.UNATTESTED, f"{e['label']} scored {c}"


def test_the_three_coverage_tiers_partition_the_vocabulary() -> None:
    """No domain may be in two tiers, and none may be in none.

    A domain silently absent from all three would be a live feature with no test
    coverage of its own existence.
    """
    tiers = [set(S.ATTESTED), set(S.SUPPORTED), set(S.UNATTESTED)]
    everything = set(S._VOCAB)
    for a, b in ((0, 1), (0, 2), (1, 2)):
        assert not (tiers[a] & tiers[b]), tiers[a] & tiers[b]
    covered = set().union(*tiers)
    assert covered == everything, f"uncovered: {everything - covered}"


# ------------------------------------------------------------- shape tiebreak


def _profile_for(name: str, kind: str, distinct: int, values=()) -> dict:
    return {"fields": [{"field": name, "kind": kind, "distinct": distinct,
                        "shape": {"values": [{"value": v, "n": 1} for v in values]}}]}


def test_uppercase_short_values_settle_an_ambiguous_ticker() -> None:
    """`symbol` is a ticker in one dataset and a chemical symbol in another.

    A schema that only weakly says "markets", plus a categorical column of 120
    short uppercase distinct values, is a ticker and nothing else.
    """
    schema = [{"name": "symbol", "type": "string",
               "description": "Exchange ticker symbol identifying the stock",
               "required": False}]
    out = S.sector(schema, profile=_profile_for(
        "symbol", "categorical", 120, ["NSE", "BSE", "HUL", "TCS", "INFY"]))
    assert out["sector"] == "markets", out
    assert out["method"] == "schema+shape"


def test_a_company_name_column_is_not_a_ticker_column() -> None:
    """Same cardinality, different values. This is the tiebreaker's whole job."""
    schema = [{"name": "symbol", "type": "string",
               "description": "Exchange ticker symbol identifying the stock",
               "required": False}]
    out = S.sector(schema, profile=_profile_for(
        "symbol", "categorical", 120,
        ["Onelife Capital Advisors Limited", "Quality Power Infra Limited"]))
    assert out["method"] != "schema+shape", out


def test_shape_alone_cannot_carry_a_sector() -> None:
    schema = [{"name": "label", "type": "string", "description": "Label",
               "required": False}]
    out = S.sector(schema, profile=_profile_for(
        "label", "categorical", 500, ["AA", "BB", "CC"]))
    assert out["sector"] != "markets", out
    assert out["sector"] != "venture", out


# ---------------------------------------------------------------- goal tiebreak


def test_an_incidental_goal_word_is_not_a_tiebreak() -> None:
    assert S.sector([{"name": "a", "description": "A"}], goal="a list of things")["sector"] is None


def test_records_route_agrees_with_the_schema_route() -> None:
    """The dashboard aggregate and the dataset page must not disagree.

    One goes through `sector_for_records` with records in hand, the other through
    `sector` with the profiler's output. Both must reach the same answer for the
    same dataset, or the roll-up and the panel will contradict each other.
    """
    entry = by_prefix("Enumerate up to 50 companies")[0]
    via_schema = S.sector(entry["fields"], goal=entry["label"])
    # Records carrying only the strong signal, nothing else.
    records = [{"fields": {"yc_slug": {"value": "acme-ai"}},
                "fields": {"batch_season": {"value": "W26"}}},
               {"fields": {"yc_slug": {"value": "beta-io"}},
                "fields": {"batch_season": {"value": "W26"}}}]
    via_records = S.sector_for_records(entry["fields"], records, goal=entry["label"])
    assert via_records["sector"] == via_schema["sector"] == "venture"
