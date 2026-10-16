"""Tests for the read cache.

The behaviours worth pinning are the ones where a cache silently returns the
wrong answer rather than merely a slow one:

* an exception must not be cached, or one transient blip becomes a TTL outage;
* different keys must not see each other's values;
* `invalidate` must actually clear, or writes become invisible until expiry;
* concurrent callers must not each run the compute.
"""
import threading
import time

import pytest

from app.services import readcache


@pytest.fixture(autouse=True)
def _clear():
    readcache.invalidate()
    yield
    readcache.invalidate()


def test_a_second_call_is_served_from_the_cache():
    calls = []

    def factory():
        calls.append(1)
        return {"answer": 42}

    first, hit, shared = readcache.cached("k", factory)
    assert (first, hit, shared) == ({"answer": 42}, False, False)

    second, hit2, _ = readcache.cached("k", factory)
    assert second == {"answer": 42}
    assert hit2 is True
    assert len(calls) == 1, "the factory ran twice; the cache is not caching"


def test_an_expired_entry_is_recomputed():
    calls = []
    value, hit, _ = readcache.cached("k", lambda: calls.append(1) or "a",
                                    ttl_s=0.05)
    assert (value, hit) == ("a", False)
    time.sleep(0.1)
    value, hit, _ = readcache.cached("k", lambda: calls.append(1) or "a",
                                    ttl_s=0.05)
    assert (value, hit) == ("a", False)
    assert len(calls) == 2


def test_an_exception_is_not_cached():
    """A cached exception turns one blip into a TTL-long outage."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("db blip")

    with pytest.raises(RuntimeError):
        readcache.cached("k", boom)

    # The next caller must be able to succeed rather than inherit the failure.
    value, hit, _ = readcache.cached("k", lambda: "recovered")
    assert (value, hit) == ("recovered", False)
    assert len(calls) == 1


def test_distinct_keys_do_not_share_values():
    a, _, _ = readcache.cached("dataset:a", lambda: "A")
    b, _, _ = readcache.cached("dataset:b", lambda: "B")
    assert (a, b) == ("A", "B")


def test_invalidate_clears_everything():
    readcache.cached("gaps:1", lambda: "x")
    readcache.cached("coverage:1", lambda: "y")
    assert readcache.invalidate() == 2
    _, hit, _ = readcache.cached("gaps:1", lambda: "x2")
    assert hit is False


def test_prefix_invalidate_spares_other_datasets():
    readcache.cached("gaps:1", lambda: "x")
    readcache.cached("gaps:2", lambda: "y")
    assert readcache.invalidate("gaps:1") == 1
    assert readcache.cached("gaps:2", lambda: "y2")[1] is True, \
        "invalidate(prefix) must not drop another dataset's entry"
    assert readcache.cached("gaps:1", lambda: "x2")[1] is False


def test_a_concurrent_burst_computes_once():
    """The stampede is the case a TTL does not cover."""
    calls = []
    release = threading.Event()

    def slow():
        calls.append(1)
        release.wait(timeout=5)
        return "value"

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(readcache.cached("k", slow)))
        for _ in range(12)]
    for t in threads:
        t.start()
    time.sleep(0.15)  # let the leader reach the factory
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1, f"factory ran {len(calls)}x; single-flight is broken"
    assert all(r[0] == "value" for r in results)
    # Every caller except the leader must be marked as having shared.
    assert sum(1 for r in results if r[2]) == 11


def test_a_waiter_that_times_out_computes_rather_than_hanging():
    """A hung leader must not pin the other callers forever."""
    readcache.SINGLE_FLIGHT_S = 0.05
    release = threading.Event()

    def hung():
        release.wait(timeout=5)
        return "leader"

    leader = threading.Thread(target=lambda: readcache.cached("k", hung))
    leader.start()
    time.sleep(0.1)

    value, _hit, shared = readcache.cached("k", lambda: "waiter")
    release.set()
    leader.join(timeout=10)
    assert value == "waiter"
    assert shared is True


def test_stats_never_expose_a_cached_payload():
    readcache.cached("gaps:1", lambda: {"secret": "value"})
    stats = readcache.stats()
    assert stats["entries"] == 1
    assert "secret" not in repr(stats)