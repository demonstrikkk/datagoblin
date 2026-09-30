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


# --- single flight ------------------------------------------------------------
# The TTL only helps *after* the first request finishes. The dashboard is the
# landing page, so the first thing anyone does with a cold cache is open it three
# ways at once, and three identical 20-30s computes against the same remote
# database is how a slow page also becomes a slow database. These tests drive real
# threads because a single-threaded fake would pass against a broken
# implementation.

def test_concurrent_cold_reads_compute_once():
    import threading

    class _Slow(_Store):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def list_datasets(self, limit=50):
            self.calls += 1
            time.sleep(0.25)
            return super().list_datasets(limit)

    dashboard_svc.invalidate()
    s = _Slow()
    results = []
    threads = [threading.Thread(target=lambda: results.append(dashboard_svc.summarise(s)))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 6
    assert s.calls == 1, f"the aggregate was computed {s.calls} times under a stampede"


def test_a_waiter_is_told_it_shared_somebody_elses_compute():
    """So a slow page can be diagnosed as a stampede rather than guessed at."""
    import threading

    class _Slow(_Store):
        def list_datasets(self, limit=50):
            time.sleep(0.2)
            return super().list_datasets(limit)

    dashboard_svc.invalidate()
    s = _Slow()
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(dashboard_svc.summarise(s)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(o["shared"] for o in out) == [False, True, True, True]


def test_all_waiters_see_the_same_numbers():
    """One leader's answer, not four slightly different ones."""
    import threading

    class _Slow(_Store):
        def list_datasets(self, limit=50):
            time.sleep(0.2)
            return super().list_datasets(limit)

    dashboard_svc.invalidate()
    s = _Slow()
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(dashboard_svc.summarise(s)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    totals = {tuple(sorted(o["totals"].items())) for o in out}
    assert len(totals) == 1, "concurrent readers disagreed on the aggregate"


def test_a_failed_compute_is_not_cached_as_an_answer():
    """A waiter must get the leader's failure, not a silently different number."""
    import threading

    class _Boom(_Store):
        def list_datasets(self, limit=50):
            time.sleep(0.15)
            raise RuntimeError("database is away")

    dashboard_svc.invalidate()
    s = _Boom()
    errors: list = []
    threads = [threading.Thread(target=lambda: _try(s, errors)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 3, "a failure was swallowed by a waiter"
    # And the next call must retry rather than serve a cached nothing.
    dashboard_svc.invalidate()
    good = _Store()
    assert dashboard_svc.summarise(good)["totals"]["datasets"] == 3


def _try(store, errors):
    try:
        dashboard_svc.summarise(store)
    except Exception as exc:  # noqa: BLE001
        errors.append(type(exc).__name__)


def test_the_lock_is_not_held_across_the_compute():
    """A lock held during the compute would serialise the stampede it exists to
    prevent — every unrelated caller would queue behind one slow aggregate.

    Probed with a *different* key. A caller sharing the leader's key is supposed
    to block: waiting on somebody else's answer is the whole point of single
    flight. What must not happen is an unrelated request being stuck behind it,
    and only a distinct key can show that.
    """
    import threading

    started = threading.Event()
    release = threading.Event()

    def _slow_factory():
        started.set()
        release.wait(timeout=10)
        return "value"

    dashboard_svc.invalidate()
    out = []

    leader = threading.Thread(
        target=lambda: out.append(dashboard_svc._single_flight("key-a", _slow_factory)[0]))
    leader.start()
    assert started.wait(timeout=5), "the leader never entered the factory"

    other: list = []
    unrelated = threading.Thread(
        target=lambda: other.append(dashboard_svc._single_flight("key-b", lambda: "other")[0]))
    unrelated.start()
    unrelated.join(timeout=2)
    assert not unrelated.is_alive(), \
        "an unrelated key was blocked on the lock during the compute"

    release.set()
    leader.join(timeout=10)
    assert out == ["value"]
    assert other == ["other"]


def test_a_waiter_shares_the_leaders_answer_rather_than_computing_its_own():
    """The distinction from the test above: same key blocks and is right."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def _slow_factory():
        started.set()
        release.wait(timeout=10)
        return "value"

    out: list = []
    leader = threading.Thread(
        target=lambda: out.append(dashboard_svc._single_flight("key-c", _slow_factory)[0]))
    leader.start()
    assert started.wait(timeout=5)

    waiter = threading.Thread(
        target=lambda: out.append(dashboard_svc._single_flight("key-c", lambda: "other")[0]))
    waiter.start()
    waiter.join(timeout=1)
    assert waiter.is_alive(), "a same-key waiter did not block on the in-flight compute"

    release.set()
    waiter.join(timeout=10)
    assert out == ["value", "value"], out
