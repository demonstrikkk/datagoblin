"""Tests for the reusable browser pool.

These use a fake crawler rather than a real one, deliberately. The behaviour
worth pinning here is lifecycle -- recycle, idle close, invalidate-on-failure,
and the flag being off -- and none of it is about Playwright. A test that needs
a browser would be slow, flaky, and unable to exercise the paths that matter
most (a wedged browser, a failed close, a timed-out call).

What is deliberately NOT proven here: that crawl4ai's AsyncWebCrawler is safe to
reuse across `arun` calls. That needs a real browser and a real site, and is
the reason the flag defaults to off.
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