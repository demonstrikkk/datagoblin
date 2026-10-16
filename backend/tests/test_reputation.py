"""Cross-run trust in a source, and how stale a dataset is.

Both features are arithmetic over stored counters, so they are tested as
arithmetic: the ordering claims are asserted against concrete host profiles,
because "it returns a number in [0,1]" would pass while every number was wrong.

The ordering assertions are the point. A trust score that cannot separate
"excellent and still answering" from "excellent two years ago" or from "answers
constantly and proves nothing" is not doing the job it was added for, and those
three are indistinguishable to the per-dataset yield ranking this replaces.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.services import reputation as R

NOW = dt.datetime(2026, 10, 13, tzinfo=dt.UTC)


def ago(days: float) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


def host(name: str, visits: int, successes: int, cells: int, verified: int,
         blocks: int = 0, success_days_ago: float = 1.0) -> dict:
    return {"host": name, "visits": visits, "successes": successes,
            "cells": cells, "verified_cells": verified, "block_count": blocks,
            "last_seen": ago(success_days_ago),
            "last_success_at": ago(success_days_ago)}


# ------------------------------------------------------------------- ordering


def test_a_currently_excellent_host_outranks_a_stale_one():
    """The distinction the feature exists for.

    Identical counters; the only difference is age. Under a yield-only ranking
    these two are tied, so a refresh re-reads whichever sorts first and can
    spend the budget on a source that stopped answering long ago.
    """
    good = R.trust(host("good.example", 40, 39, 800, 760), NOW)
    stale = R.trust(host("stale.example", 40, 39, 800, 760,
                         success_days_ago=700), NOW)
    assert good["score"] > stale["score"] * 3, (
        f"age barely moved the score ({good['score']} vs {stale['score']}); "
        f"decay is not being applied")
    assert stale["terms"]["decay"] < 0.05


def test_a_host_that_answers_but_proves_nothing_does_not_outrank_one_that_proves():
    """Volume is not quality. This is what a yield-only ranking gets wrong."""
    proving = R.trust(host("proving.example", 30, 30, 400, 380), NOW)
    chatty = R.trust(host("chatty.example", 30, 30, 400, 20), NOW)
    assert proving["score"] > chatty["score"]


def test_blocks_drive_a_host_to_the_bottom():
    blocked = R.trust(host("blocked.example", 30, 5, 50, 10, blocks=12,
                           success_days_ago=2), NOW)
    assert blocked["score"] == 0.0
    assert blocked["terms"]["block_penalty"] == R.MAX_BLOCK_PENALTY


def test_block_penalty_is_capped():
    """A temporary block must not retire a host permanently."""
    once = R.trust(host("x.example", 40, 40, 400, 380, blocks=1), NOW)
    many = R.trust(host("y.example", 40, 40, 400, 380, blocks=500), NOW)
    assert once["score"] - many["score"] <= R.MAX_BLOCK_PENALTY + 1e-9
    assert many["terms"]["block_penalty"] == R.MAX_BLOCK_PENALTY


def test_one_lucky_fetch_does_not_beat_a_proven_record():
    """A single page cannot be a reputation."""
    lucky = R.trust(host("lucky.example", 1, 1, 5, 5), NOW)
    proven = R.trust(host("proven.example", 40, 39, 800, 760), NOW)
    assert lucky["terms"]["low_confidence"] is True
    assert proven["terms"]["low_confidence"] is False
    assert proven["score"] > lucky["score"]


def test_a_host_we_have_never_fetched_is_unknown_not_bad():
    unknown = R.trust({"host": "new.example", "visits": 0}, NOW)
    assert unknown["known"] is False
    assert unknown["score"] == 0.0
    assert unknown["reason"] == "never fetched"


def test_rank_hosts_is_ordered_and_stable():
    rows = [host("b.example", 30, 30, 400, 380),
            host("a.example", 30, 30, 400, 380),
            host("c.example", 30, 3, 400, 5)]
    first = [s["host"] for s in R.rank_hosts(rows, NOW)]
    # a and b have identical scores, so the tie must break by name, not by input
    # order — otherwise two identical calls can return different rankings.
    assert first[0] == "a.example" and first[1] == "b.example"
    assert first[2] == "c.example"
    assert [s["host"] for s in R.rank_hosts(list(reversed(rows)), NOW)] == first


def test_every_term_is_reported_alongside_the_score():
    """An unexplained number cannot justify a deprioritised source."""
    scored = R.trust(host("x.example", 40, 39, 800, 760), NOW)
    for key in ("proof", "reachability", "block_penalty", "decay", "low_confidence"):
        assert key in scored["terms"], f"{key} missing from the explanation"
    assert 0.0 <= scored["score"] <= 1.0


@pytest.mark.parametrize("row", [
    {"visits": 0}, {"visits": 5, "successes": 0, "cells": 0},
    {"visits": 1, "successes": 1, "cells": 0, "verified_cells": 0},
])
def test_degenerate_rows_do_not_raise(row):
    scored = R.trust(row, NOW)
    assert 0.0 <= scored["score"] <= 1.0


# --------------------------------------------------------- timestamp handling


def test_a_string_timestamp_is_accepted():
    """Repositories disagree: psycopg returns datetime, JSON round-trips a string.

    Both reach this code. An `AttributeError` on the string form is what a live
    staleness endpoint returned before this was handled.
    """
    row = {"host": "x.example", "visits": 10, "successes": 10, "cells": 100,
           "verified_cells": 50, "block_count": 0,
           "last_seen": "2026-10-12T00:00:00+00:00",
           "last_success_at": "2026-10-12T00:00:00+00:00"}
    scored = R.trust(row, NOW)
    assert scored["score"] > 0
    assert scored["terms"]["decay"] > 0.9


def test_a_naive_timestamp_is_treated_as_utc():
    row = {"host": "x.example", "visits": 10, "successes": 10, "cells": 100,
           "verified_cells": 50, "last_success_at": dt.datetime(2026, 10, 12)}
    assert R.trust(row, NOW)["terms"]["decay"] > 0.9


@pytest.mark.parametrize("bad", [None, "", "not a date", {}, []])
def test_an_unparseable_timestamp_reads_as_no_evidence(bad):
    assert R._as_datetime(bad) is None


# ----------------------------------------------------------------- staleness


def test_field_thresholds_differ_by_volatility():
    """One global number would either stale a name or excuse a stale number."""
    assert R.stale_after_days("annual_revenue") < R.stale_after_days("company_name")
    assert R.stale_after_days("funding_amount") < R.stale_after_days("ceo")
    assert R.stale_after_days("some_unknown_column") == R.DEFAULT_STALE_DAYS


def test_a_never_verified_field_is_the_most_stale_thing():
    never = {"field": "revenue", "last_verified_at": None}
    ancient = {"field": "revenue", "last_verified_at": ago(400)}
    assert R.is_stale(never, NOW)
    assert R.is_stale(ancient, NOW)
    assert not R.is_stale({"field": "revenue", "last_verified_at": ago(1)}, NOW)


def test_a_stable_field_is_not_called_stale_after_a_year_of_nothing():
    """Company names do not go stale the way revenue does."""
    row = {"field": "company_name", "last_verified_at": ago(300)}
    assert not R.is_stale(row, NOW), (
        "a 300-day-old company name is not stale data; calling it stale sends "
        "refreshes at the wrong fields")
    fast = {"field": "annual_revenue", "last_verified_at": ago(30)}
    assert R.is_stale(fast, NOW)


def test_dataset_staleness_reports_the_weakest_field_not_the_average():
    """An average hides the one field that is two years old."""
    fields = [
        {"field": "company_name", "last_verified_at": ago(1)},
        {"field": "ceo", "last_verified_at": ago(2)},
        {"field": "annual_revenue", "last_verified_at": ago(300)},
        {"field": "latest_funding", "last_verified_at": None},
    ]
    out = R.dataset_staleness(fields, NOW)
    assert out["oldest_age_days"] == 300.0
    assert out["stale_count"] == 2
    assert set(out["stale_fields"]) == {"annual_revenue", "latest_funding"}
    assert out["never_verified_fields"] == ["latest_funding"]
    assert out["field_count"] == 4
    assert out["stale_pct"] == 50.0


def test_dataset_staleness_of_nothing_is_zero_not_an_error():
    out = R.dataset_staleness([], NOW)
    assert out["field_count"] == 0
    assert out["stale_pct"] == 0.0
    assert out["oldest_age_days"] is None


def test_refresh_order_puts_never_verified_first():
    """An unverified field is the only one where a read certainly adds something."""
    fields = [
        {"field": "ceo", "last_verified_at": ago(1)},
        {"field": "annual_revenue", "last_verified_at": ago(60)},
        {"field": "unknown_field", "last_verified_at": None},
        {"field": "company_name", "last_verified_at": ago(10)},
    ]
    order = R.refresh_order(fields, NOW)
    assert order[0] == "unknown_field"
    assert order.index("annual_revenue") < order.index("ceo")


def test_refresh_order_is_deterministic():
    fields = [{"field": "a", "last_verified_at": ago(5)},
              {"field": "b", "last_verified_at": ago(5)}]
    assert R.refresh_order(fields, NOW) == R.refresh_order(fields, NOW)