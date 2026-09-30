
"""Dated fields, and the same place written three ways."""

from __future__ import annotations

from app.services import profile as profile_svc
from tests.test_profile import _Store, _cell, _p


def test_a_date_field_becomes_a_timeline_not_forty_categories():
    """`last_verified_date` profiled as 43 separate bars, one per date: a wall of
    noise describing the shape of nothing."""
    records = [{"record_id": str(i),
                "fields": {"verified_on": _cell(f"2020-{m:02d}-15")}}
               for i, m in enumerate(range(1, 7), 1)]
    f = _p(records, [{"name": "verified_on"}])["fields"][0]
    assert f["kind"] == "date"
    assert f["shape"]["min"] == 2020 and f["shape"]["max"] == 2020
    assert f["shape"]["by_year"][0]["n"] == 6


def test_a_span_of_years_is_filled_in_so_a_gap_is_visible():
    records = [{"record_id": "a", "fields": {"year": _cell("2019")}},
               {"record_id": "b", "fields": {"year": _cell("2022")}}]
    f = _p(records, [{"name": "year"}])["fields"][0]
    assert f["kind"] == "date"
    years = {b["year"]: b["n"] for b in f["shape"]["by_year"]}
    assert years == {2019: 1, 2020: 0, 2021: 0, 2022: 1}


def test_a_bare_four_digit_number_is_a_year_only_when_the_field_says_so():
    """`founded_year` holding `1998` is a point in time. `market_cap` holding
    `1500` is a quantity, and reading it as the year 1500 would turn a revenue
    histogram into a four-century timeline. The field name decides."""
    years = [{"record_id": str(i), "fields": {"founded_year": _cell(str(1990 + i))}}
             for i in range(1, 12)]
    f = _p(years, [{"name": "founded_year"}])["fields"][0]
    assert f["kind"] == "date"
    assert f["shape"]["min"] == 1991 and f["shape"]["max"] == 2001

    money = [{"record_id": str(i), "fields": {"market_cap": _cell(str(1500 + i))}}
             for i in range(1, 12)]
    f = _p(money, [{"name": "market_cap"}])["fields"][0]
    assert f["kind"] == "numeric"
    assert f["shape"]["min"] == 1501 and f["shape"]["max"] == 1511


def test_a_phrase_is_not_forced_into_a_date():
    records = [{"record_id": "a", "fields": {"d": _cell("roughly 2019")}},
               {"record_id": "b", "fields": {"d": _cell("recently")}},
               {"record_id": "c", "fields": {"d": _fields_none()}}]
    out = _p(records, [{"name": "d"}])
    f = out["fields"][0]
    assert f["kind"] != "date"


def _fields_none():
    return ""


def test_month_names_are_understood():
    records = [{"record_id": "a", "fields": {"d": _cell("15 March 2021")}},
               {"record_id": "b", "fields": {"d": _cell("Jan 3, 2022")}}]
    f = _p(records, [{"name": "d"}])["fields"][0]
    assert f["kind"] == "date"
    assert f["shape"]["min"] == 2021 and f["shape"]["max"] == 2022


# --- the same place, three ways ---------------------------------------------

def test_one_city_written_three_ways_is_counted_as_one():
    """`London` (23), `London, England` (14) and `London, UK` (4) profiled as
    three answers, which made the top city look like the third most common."""
    vals = ["London"] * 23 + ["London, England"] * 14 + ["London, UK"] * 4
    records = [{"record_id": str(i), "fields": {"hq": _cell(v)}}
               for i, v in enumerate(vals)]
    out = _p(records, [{"name": "hq"}])
    place = out["place"]
    assert place["field"] == "hq"
    assert place["values"][0] == {"value": "London", "n": 41}
    assert place["variants"] == 2
    assert place["as_written_count"] == 3


def test_the_raw_reading_is_still_reported_because_the_two_disagree():
    vals = ["London"] * 3 + ["London, England"] * 2
    records = [{"record_id": str(i), "fields": {"hq": _cell(v)}}
               for i, v in enumerate(vals)]
    raw = _p(records, [{"name": "hq"}])["place"]["raw"]
    assert {r["value"] for r in raw} == {"London", "London, England"}


def test_consolidation_cannot_invent_a_place():
    merged = profile_svc._consolidate(["Paris", "Paris, France", "Lyon"])
    assert {m["value"]: m["n"] for m in merged["values"]} == {"Paris": 2, "Lyon": 1}
