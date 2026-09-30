"""The field profiler: what it draws, and what it refuses to draw.

Every test here is about a refusal, because that is where a profiler is capable
of lying. A field with one value must not get a distribution; an empty field must
not get a zero-height bar; a revenue column must not be reported as a country
because its name contains "market".
"""
from __future__ import annotations

import pytest

from app.services import profile as profile_svc


def _cell(value, status="verified", url="https://x.example/a"):
    return {"value": value, "verification_status": status,
            "source": {"url": url, "quote": f"q:{value}", "page_id": "p1"}}


class _Store:
    def __init__(self, records, schema):
        self._records = records
        self._schema = schema

    def get_dataset_row(self, dataset_id):
        return {"id": dataset_id, "run_id": "r1", "schema": self._schema}

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        rows = self._records[offset:offset + limit]
        return {"records": rows, "total": len(self._records)}


def _p(records, schema):
    return profile_svc.profile(_Store(records, schema), "d1")


# --- numbers ----------------------------------------------------------------

def test_a_numeric_field_gets_a_histogram_with_real_bounds():
    out = _p([{"record_id": str(i), "fields": {"revenue": _cell(i * 10)}}
              for i in range(1, 21)], [{"name": "revenue"}])
    f = out["fields"][0]
    assert f["kind"] == "numeric"
    s = f["shape"]
    assert s["min"] == 10 and s["max"] == 200
    assert len(s["buckets"]) == profile_svc.BUCKETS
    assert sum(b["n"] for b in s["buckets"]) == 20
    assert s["median"] == pytest.approx(105)


def test_currency_and_separators_are_read_as_numbers():
    out = _p([{"record_id": "a", "fields": {"revenue": _cell("€1,200")}},
              {"record_id": "b", "fields": {"revenue": _cell("$3 000")}},
              {"record_id": "c", "fields": {"revenue": _cell("4.5%")}}],
             [{"name": "revenue"}])
    f = out["fields"][0]
    assert f["kind"] == "numeric"
    assert f["shape"]["min"] == 4.5 and f["shape"]["max"] == 3000


def test_a_phrase_that_is_not_a_number_is_not_converted():
    """Guessing what "about 40%" meant is how a histogram grows a fabricated
    bucket. It is left out of the shape and still counted as filled."""
    out = _p([{"record_id": "a", "fields": {"note": _cell("about 40%")}},
              {"record_id": "b", "fields": {"note": _cell("n/a")}},
              {"record_id": "c", "fields": {"note": _cell("loose")}},
              {"record_id": "d", "fields": {"note": _cell("varies")}}],
             [{"name": "note"}])
    f = out["fields"][0]
    assert f["values"] == 4          # four records have a value
    assert f["kind"] == "categorical"  # none of them is a number
    assert "shape" in f


# --- refusals ---------------------------------------------------------------

def test_one_distinct_value_gets_no_chart():
    """One bar is a sentence with axes. Drawing it implies a distribution."""
    out = _p([{"record_id": "a", "fields": {"status": _cell("active")}},
              {"record_id": "b", "fields": {"status": _cell("active")}}],
             [{"name": "status"}])
    f = out["fields"][0]
    assert f["kind"] == "scalar"
    assert f["shape"] == {}
    assert f["distinct"] == 1


def test_a_field_with_no_values_is_empty_not_a_zero_height_chart():
    out = _p([{"record_id": "a", "fields": {"ticker": _cell("")}}],
             [{"name": "ticker"}])
    f = next(x for x in out["fields"] if x["field"] == "ticker")
    assert f["kind"] == "empty"
    assert f["shape"] == {}
    assert f["never_extracted"] is True
    assert f["values"] == 0


def test_a_declared_field_with_no_records_at_all_is_reported_not_omitted():
    out = _p([], [{"name": "website_url"}, {"name": "company_name"}])
    assert {f["field"] for f in out["fields"]} == {"website_url", "company_name"}
    assert out["totals"]["never_extracted"] == 2


def test_a_constant_numeric_field_gets_no_histogram():
    out = _p([{"record_id": "a", "fields": {"rank": _cell(1)}},
              {"record_id": "b", "fields": {"rank": _cell(1)}}],
             [{"name": "rank"}])
    f = out["fields"][0]
    assert f["shape"] == {}


# --- categorical ------------------------------------------------------------

def test_a_categorical_field_lists_values_biggest_first():
    out = _p([{"record_id": str(i), "fields": {"sector": _cell(v)}}
              for i, v in enumerate(["tech", "tech", "tech", "bank", "energy"])],
             [{"name": "sector"}])
    s = out["fields"][0]["shape"]
    assert [v["value"] for v in s["values"]] == ["tech", "bank", "energy"]
    assert [v["n"] for v in s["values"]] == [3, 1, 1]
    assert s["truncated"] is False


def test_a_long_tail_is_truncated_and_says_so():
    out = _p([{"record_id": str(i), "fields": {"city": _cell(f"city-{i}")}}
              for i in range(40)], [{"name": "city"}])
    s = out["fields"][0]["shape"]
    assert len(s["values"]) == 12
    assert s["truncated"] is True


def test_values_differing_only_in_case_or_padding_count_as_one():
    out = _p([{"record_id": "a", "fields": {"exchange": _cell("NSE")}},
              {"record_id": "b", "fields": {"exchange": _cell("nse")}},
              {"record_id": "c", "fields": {"exchange": _cell(" NSE ")}}],
             [{"name": "exchange"}])
    assert out["fields"][0]["distinct"] == 1


# --- trust ------------------------------------------------------------------

def test_trust_comes_from_coverage_so_the_two_views_cannot_disagree():
    records = [
        {"record_id": "a", "fields": {"x": _cell("1")}},
        {"record_id": "b", "fields": {"x": _cell("2", status="unverified")}},
        {"record_id": "c", "fields": {"x": _cell("3", status="conflicting")}},
        {"record_id": "d", "fields": {}},
    ]
    f = _p(records, [{"name": "x"}])["fields"][0]
    assert f["trust"]["present"] == 3
    assert f["trust"]["missing"] == 1
    assert f["trust"]["verified"] == 1
    assert f["trust"]["unverified"] == 1
    assert f["trust"]["conflicting"] == 1
    assert f["fully_filled"] is False


def test_a_sampled_profile_says_it_was_sampled():
    class _Big(_Store):
        def get_records(self, dataset_id, q="", limit=100, offset=0):
            rows = [{"record_id": str(i), "fields": {"x": _cell(str(i))}}
                    for i in range(50)]
            return {"records": rows[:limit], "total": 50}

    store = _Big([], [{"name": "x"}])
    out = profile_svc.profile(store, "d1", None, 10)
    assert out["sampled"] is True
    assert out["records"] == 50 and out["records_read"] == 10


# --- the place field --------------------------------------------------------

def test_a_country_field_becomes_the_place_field():
    records = [{"record_id": str(i), "fields": {"headquarters_country": _cell(c)}}
               for i, c in enumerate(["India", "India", "Germany", "France"])]
    out = _p(records, [{"name": "headquarters_country"}])
    assert out["place"]["field"] == "headquarters_country"
    assert out["place"]["values"][0] == {"value": "India", "n": 2}


def test_revenue_is_not_a_country_despite_the_word_market():
    """`market_cap` matches the "market" hint. Reporting a distribution of
    revenue as a map would be the worst possible version of this feature."""
    records = [{"record_id": str(i), "fields": {"market_cap": _cell(v)}}
               for i, v in enumerate(["€1M", "€9M", "€40M"])]
    out = _p(records, [{"name": "market_cap"}])
    assert out["place"] is None


def test_no_place_field_means_none_rather_than_an_empty_list():
    out = _p([{"record_id": "a", "fields": {"company_name": _cell("Acme")}}],
             [{"name": "company_name"}])
    assert out["place"] is None


def test_a_country_field_with_one_value_is_not_a_place_distribution():
    out = _p([{"record_id": "a", "fields": {"country": _cell("India")}},
              {"record_id": "b", "fields": {"country": _cell("India")}}],
             [{"name": "country"}])
    assert out["place"] is None
