"""The dashboard aggregate is 26 seconds of reads, so it is cached and invalidated.

Measured at 26s for 15 datasets. A summary page that takes 26 seconds to show a
number someone reads at a glance is not a summary page, and recomputing the
identical answer on every navigation is waste rather than freshness — the
underlying data only changes when a run finishes or a write is applied, and both
of those drop the cache explicitly.
"""
from __future__ import annotations

import time

from app.services import dashboard as dashboard_svc


class _Store:
    def __init__(self):
        self.reads = 0

    def list_datasets(self, limit=50):
        self.reads += 1
        return [{"id": f"d{i}", "run_id": "r", "name": f"n{i}", "schema": [],
                 "record_count": 3} for i in range(3)]

    def get_records(self, dataset_id, q="", limit=100, offset=0):
        self.reads += 1
        return {"records": [{"record_id": "1", "fields": {"x": "v"}}], "total": 1}

    def get_sources(self, dataset_id):
        self.reads += 1
        return {"sources": []}

    def get_pages(self, run_id, limit=200):
        self.reads += 1
        return []


def test_a_second_call_within_the_window_is_served_from_cache():
    dashboard_svc.invalidate()
    s = _Store()
    first = dashboard_svc.summarise(s)
    after = s.reads
    second = dashboard_svc.summarise(s)
    assert s.reads == after, "the aggregate was recomputed"
    assert second["totals"] == first["totals"]


def test_the_cached_copy_says_how_old_it_is():
    """A summary page that cannot say it is showing you something a minute old
    is asking you to trust a number you cannot place in time."""
    dashboard_svc.invalidate()
    s = _Store()
    dashboard_svc.summarise(s)
    time.sleep(0.05)
    assert dashboard_svc.summarise(s)["cached_age_s"] > 0.0


def test_invalidate_forces_a_recompute():
    dashboard_svc.invalidate()
    s = _Store()
    dashboard_svc.summarise(s)
    before = s.reads
    dashboard_svc.invalidate()
    dashboard_svc.summarise(s)
    assert s.reads > before


def test_use_cache_false_always_recomputes():
    dashboard_svc.invalidate()
    s = _Store()
    dashboard_svc.summarise(s)
    before = s.reads
    dashboard_svc.summarise(s, use_cache=False)
    assert s.reads > before


def test_a_different_limit_is_not_served_the_other_answer():
    """The limit is part of the answer, not part of the request."""
    dashboard_svc.invalidate()
    s = _Store()
    assert dashboard_svc.summarise(s, 1)["totals"]["datasets"] == 1
    assert dashboard_svc.summarise(s, 3)["totals"]["datasets"] == 3


def test_the_compute_time_is_reported_rather_than_hidden():
    dashboard_svc.invalidate()
    out = dashboard_svc.summarise(_Store())
    assert "compute_ms" in out
