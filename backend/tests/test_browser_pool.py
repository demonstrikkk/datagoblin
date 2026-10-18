"""Tests for the reusable browser pool.

These use a fake crawler rather than a real one, deliberately. The behaviour
worth pinning here is lifecycle -- recycle, idle close, invalidate-on-failure,
and the flag being off -- and none of it is about Playwright. A test that needs
a browser would be slow, flaky, and unable to exercise the paths that matter
most (a wedged browser, a failed close, a timed-out call).

What is NOT proven here: anything about Playwright itself. That was true until
a live crawl checked it -- see docs/39-REUSE-VERIFICATION.md. Against a real
Chromium and three real pages, one browser reused across many `arun` calls
produced output inside the band two fresh browsers produced for the same pages,
and every lifecycle bound (recycle, invalidate, idle reap) behaved. Nothing
reuse-specific failed. The flag still defaults off, but now for the measured
reason -- the speed payoff is unproven, not the safety. What is still unknown
is why the Playwright driver dies mid-render on some pages; it does so with the
flag off as well, so it is not this module's doing.
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.crawl.browser_pool import BrowserPool  # noqa: E402


def sync(coro_fn):
    import functools

    @functools.wraps(coro_fn)
    def wrapper(*a, **k):
        return asyncio.run(coro_fn(*a, **k))
    return wrapper


class FakeCrawler:
    """Counts launches and closes so recycling is observable."""

    def __init__(self, gen, index):
        self.gen = gen
        self.index = index
        self.closed = False
        self.runs = 0
        self.threads: list[str] = []
        self._loop = None

    async def arun(self, url, config=None):
        if self.closed:
            raise RuntimeError("arun on a closed crawler")
        self.runs += 1
        self.threads.append(threading.current_thread().name)
        await asyncio.sleep(0)
        return {"success": True, "url": url, "crawler": self.index}

    async def close(self):
        self.closed = True
        self.gen.closed.append(self.index)


class Gen:
    def __init__(self):
        self.made = []
        self.closed = []

    async def factory(self):
        c = FakeCrawler(self, len(self.made))
        self.made.append(c)
        return c


def _job(url):
    async def run(crawler):
        return await crawler.arun(url)
    return run


# -- reuse ---------------------------------------------------------------------

def test_one_browser_serves_many_pages():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=25, idle_s=0)
    try:
        for i in range(5):
            pool.run(_job(f"https://s.example/{i}"), timeout_s=10)
        assert len(gen.made) == 1, f"launched {len(gen.made)} browsers for 5 pages"
        assert gen.made[0].runs == 5
    finally:
        pool.shutdown()


def test_the_browser_runs_on_one_dedicated_thread():
    """A Playwright browser is bound to its creating loop; this is why.

    Also proves the caller is not the one driving the page: the fetch happens
    on the pool's `dg-browser` thread, not wherever `run()` was called from.
    """
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=25, idle_s=0)
    caller = threading.current_thread().name
    try:
        pool.run(_job("https://s.example/"), timeout_s=10)
        threads = gen.made[0].threads
        assert threads, "arun recorded no thread"
        assert all(t == "dg-browser" for t in threads), \
            f"page work ran off the owning thread: {set(threads)}"
        assert caller != "dg-browser"
    finally:
        pool.shutdown()


def test_shutdown_closes_the_browser():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=25, idle_s=0)
    pool.run(_job("https://s.example/"), timeout_s=10)
    assert gen.closed == []
    pool.shutdown()
    assert gen.closed == [0], "shutdown left a browser open"


def test_shutdown_is_idempotent():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=25, idle_s=0)
    pool.run(_job("https://s.example/"), timeout_s=10)
    pool.shutdown()
    pool.shutdown()


# -- recycling -----------------------------------------------------------------

def test_a_full_browser_is_recycled_rather_than_reused():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=2, idle_s=0)
    try:
        for i in range(5):
            pool.run(_job(f"https://s.example/{i}"), timeout_s=10)
        assert len(gen.made) == 3, f"expected 3 launches for 5 pages at 2/page, got {len(gen.made)}"
        assert gen.closed == [0, 1], "a recycled browser was not closed"
    finally:
        pool.shutdown()


def test_recycling_never_exceeds_the_page_limit_per_browser():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=3, idle_s=0)
    try:
        for i in range(10):
            pool.run(_job(f"https://s.example/{i}"), timeout_s=10)
        for c in gen.made:
            assert c.runs <= 3, f"a browser served {c.runs} pages, limit was 3"
    finally:
        pool.shutdown()


# -- failure handling ----------------------------------------------------------

def test_a_failed_page_invalidates_the_browser():
    """A browser that errored must not be handed to the next caller."""
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0)

    async def boom(crawler):
        raise RuntimeError("arun failed")

    try:
        with pytest.raises(RuntimeError):
            pool.run(boom, timeout_s=10)
        assert gen.closed == [0], "the failed browser was not discarded"
        # The next call must build a fresh one rather than reuse the suspect.
        pool.run(_job("https://s.example/"), timeout_s=10)
        assert len(gen.made) == 2
    finally:
        pool.shutdown()


def test_a_timed_out_page_does_not_hold_the_caller_and_is_discarded():
    """The wedged-browser case, with both halves of the guarantee.

    The caller must be released promptly even though the page never returns --
    that is the leaked-chromium / wedged-server failure the dedicated executor
    exists to bound. And the browser must be discarded rather than reused.

    The fake sleeps 1.5s rather than forever because production bounds the page
    with `asyncio.wait_for(crawler.arun(...), timeout_s)`; only the caller's
    timeout is exercised here, not the inner one. Invalidate therefore completes
    once the loop frees, which is the real sequence.
    """
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0)

    async def hang(crawler):
        await asyncio.sleep(1.5)

    try:
        t0 = time.monotonic()
        with pytest.raises(BaseException):
            pool.run(hang, timeout_s=0.2)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"caller waited {elapsed:.1f}s on a hung page"
        assert gen.closed == [0], \
            "a wedged browser survived; this is the leaked-chromium case"
    finally:
        pool.shutdown()


def test_a_failed_close_does_not_stop_the_recycle():
    """The browser is being discarded either way; a noisy close must not wedge."""
    gen = Gen()

    async def factory():
        c = FakeCrawler(gen, len(gen.made))
        c.close = _explode
        gen.made.append(c)
        return c

    pool = BrowserPool(factory, max_pages=1, idle_s=0)
    try:
        pool.run(_job("https://s.example/0"), timeout_s=10)
        pool.run(_job("https://s.example/1"), timeout_s=10)
        assert len(gen.made) == 2, "a failed close prevented the recycle"
    finally:
        pool.shutdown()


async def _explode():
    raise RuntimeError("close failed")


def test_using_a_closed_pool_is_an_error_not_a_silent_no_op():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=25, idle_s=0)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.run(_job("https://s.example/"), timeout_s=10)


# -- idle reaping --------------------------------------------------------------

def test_an_idle_browser_is_closed_by_the_reap():
    """Reap logic, called directly.

    Deliberately not driven through the wall-clock Timer: a timer test passes
    alone and fails under parallel load, which makes it a flaky test that
    reports a real bug as intermittent. `arm_a_reap_timer` covers that the
    timer is actually scheduled.
    """
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0.15)
    try:
        pool.run(_job("https://s.example/"), timeout_s=10)
        assert gen.closed == []

        # Not yet idle by the pool's own clock.
        pool._reap()
        assert gen.closed == [], "reaped before the idle window elapsed"

        # Pretend the last use was longer ago than the window.
        pool._slots[0].touch(time.monotonic() - 10)
        pool._reap()
        assert gen.closed == [0], "the idle browser was never closed"
    finally:
        pool.shutdown()


def test_use_arms_the_idle_reap_timer():
    """The scheduling half, without waiting on the clock."""
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=30)
    try:
        assert pool._reaper is None
        pool.run(_job("https://s.example/"), timeout_s=10)
        assert pool._reaper is not None, "use did not arm the idle reap"
        assert pool._reaper.daemon, "the reap timer would block process exit"
    finally:
        pool.shutdown()


def test_a_browser_is_not_reaped_while_a_call_is_in_flight():
    """The race a live bench found, which no test here would have caught.

    The reap timer is armed once and the idle clock used to move only on
    return, so a call that ran longer than `IDLE_S` had its browser closed out
    from under it. Every later step of that render then failed with "Target
    page, context or browser has been closed" and the page fell back to a
    non-rendered rung -- a 45% content loss with no error on the stored page.

    Unreachable at the shipped defaults (`IDLE_S` 120 > the 90s crawl timeout),
    which is exactly why it survived: the numbers hid it. The window below is
    deliberately inverted, slow call against short window, because that is the
    only order in which this can be seen.
    """
    gen = Gen()
    idle_s = 0.15
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=idle_s)
    errors: list[BaseException] = []

    def slow_job(crawler):
        async def run(c):
            started.set()
            await asyncio.sleep(idle_s * 6)  # far outlives the idle window
            return await c.arun("https://s.example/slow")
        return run(crawler)

    started = threading.Event()
    try:
        # One quick call so the reap timer is armed before the slow one begins.
        pool.run(_job("https://s.example/warm"), timeout_s=10)
        assert pool._reaper is not None

        def work():
            try:
                pool.run(slow_job, timeout_s=20)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        assert started.wait(5), "the slow call never started"
        assert pool.stats()[0]["busy"] == 1, "the slot did not mark itself busy"

        # The armed timer has now fired several times over a call that is still
        # running. It must skip this slot.
        time.sleep(idle_s * 4)
        assert gen.closed == [], "reaped a browser that was mid-render"
        pool._reap()
        assert gen.closed == [], "an explicit reap closed a busy slot"

        t.join(timeout=20)
        assert not t.is_alive(), "the in-flight call never returned"
        assert errors == [], f"the in-flight call failed: {errors!r}"
        assert len(gen.made) == 1, "the in-flight call launched a second browser"
        assert pool.stats()[0]["busy"] == 0, "busy was never released"
    finally:
        pool.shutdown()


def test_the_idle_clock_starts_when_a_call_begins():
    """A call that ends exactly at the window edge is idle, not mid-render."""
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=30)
    try:
        pool.run(_job("https://s.example/"), timeout_s=10)
        pool._slots[0].touch(time.monotonic() - 31)
        pool._reap()
        assert gen.closed == [0], "an idle browser was not reaped"
    finally:
        pool.shutdown()


def test_the_pool_is_reusable_after_a_reap():
    """Reaping must not leave the pool wedged for the next real use."""
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0.15)
    try:
        pool.run(_job("https://s.example/0"), timeout_s=10)
        pool._slots[0].touch(time.monotonic() - 10)
        pool._reap()
        assert gen.closed == [0]

        out = pool.run(_job("https://s.example/1"), timeout_s=10)
        assert out["success"] is True
        assert len(gen.made) == 2, "the pool did not rebuild after reaping"
    finally:
        pool.shutdown()


def test_a_zero_idle_ttl_never_reaps_on_its_own():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0)
    try:
        pool.run(_job("https://s.example/"), timeout_s=10)
        time.sleep(0.3)
        assert gen.closed == [], "idle_s=0 reaped anyway"
    finally:
        pool.shutdown()


# -- concurrency ---------------------------------------------------------------

def test_concurrent_callers_do_not_launch_a_browser_each():
    """The failure this exists to prevent: N callers, N Chromium processes."""
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=100, idle_s=0)
    results = []
    try:

        def work(i):
            results.append(pool.run(_job(f"https://s.example/{i}"), timeout_s=20))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)
        assert len(results) == 8
        assert len(gen.made) == 1, f"8 concurrent calls launched {len(gen.made)} browsers"
    finally:
        pool.shutdown()


def test_extra_slots_are_built_and_never_used():
    """REUSE_BROWSER_CONCURRENCY > 1 does NOT buy concurrency. Pinned, not fixed.

    `BrowserPool.run()` dispatches to `self._slots[0]`, so N slots are built and
    only the first is ever used. The docstring here and the `config.py` comment
    both used to promise the opposite -- "raising it buys concurrency at the
    cost of one Chromium per slot" -- and the test above passes *because* of the
    serialisation rather than in spite of it, so nothing could fail on the lie.

    A live bench measured it (outputs/bench_reuse_isolate.json): 3 slots built,
    1 holding a browser, 2 permanently empty.

    This test is named for the truth rather than the intention, so that anyone
    who wires up a real round-robin has to come here and delete it deliberately.
    If it ever fails because extra slots DO hold browsers, that is the feature
    landing -- update the docstring and the config comment in the same change.
    """
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=50, idle_s=0, concurrency=3)
    try:
        for i in range(3):
            pool.run(_job(f"https://s.example/{i}"), timeout_s=10)
        stats = pool.stats()
        assert len(stats) == 3, f"expected 3 slots, got {len(stats)}"
        used = [i for i, s in enumerate(stats) if s["has_browser"]]
        assert used == [0], f"extra slots are now in use ({used}); wire this " \
                            f"through, then fix the docstring and config.py"
        assert len(gen.made) == 1, "each slot launched its own browser"
    finally:
        pool.shutdown()


def test_stats_never_expose_a_browser_object():
    gen = Gen()
    pool = BrowserPool(gen.factory, max_pages=7, idle_s=0)
    try:
        pool.run(_job("https://s.example/"), timeout_s=10)
        stats = pool.stats()
        assert stats[0]["max_pages"] == 7
        assert not any(isinstance(v, FakeCrawler) for v in stats[0].values())
    finally:
        pool.shutdown()