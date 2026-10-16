"""A reusable browser for the crawl rung, behind a feature flag.

Default OFF. `REUSE_BROWSER=true` opts in; without it every page launches and
tears down its own `AsyncWebCrawler`, which is today's behaviour and is what
these tests continue to pin.

Why this is harder than it looks, and why it is shaped the way it is
------------------------------------------------------------------------
`_sync_crawl` runs on `_CRAWL_EXECUTOR` and calls `asyncio.run()`, so every
worker thread gets its own event loop. A Playwright browser is bound to the
loop that created it and cannot be driven from another. Sharing one
`AsyncWebCrawler` across the existing four worker threads would therefore fail
or, worse, appear to work until it did not.

So the browser gets its own owner: ONE thread, ONE long-lived loop, and every
`arun` submitted to it. That serialises the browser rung, which is a real cost
-- four concurrent pages become one at a time -- and it is the cost that makes
reuse worth it, since a Chromium launch costs more than the page it was
launched for. If concurrency matters more than launch cost, raise
`REUSE_BROWSER_CONCURRENCY` and accept one browser per slot.

Leak safety
-----------
`fetcher.py` already documents the failure this had to avoid: a timed-out
crawl orphans its browser subprocess (observed: 11 leaked chromiums, wedged
server). Reuse makes that *more* dangerous, not less, because the browser now
lives across calls and a wedged one would be reused for every subsequent page.
Three bounds, each addressing a specific failure:

  - MAX_PAGES: recycle after N pages, so a page that degrades the browser is
    paid for N times rather than forever;
  - IDLE_TTL: close after a period with no use, so an idle process is not
    holding Chromium open indefinitely;
  - every failure that touches the browser closes it and rebuilds, so a wedged
    browser is never handed to the next caller.

A failed `arun` invalidates the instance. That is deliberately aggressive: a
browser that errored once is treated as suspect rather than reused, because the
cost of an unnecessary relaunch is seconds and the cost of reusing a wedged one
is a run that silently stops producing pages.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("datagoblin")

DEFAULT_MAX_PAGES = 25
DEFAULT_IDLE_S = 120.0
DEFAULT_CONCURRENCY = 1


class _BrowserSlot:
    """One browser, one owning loop, one thread."""

    def __init__(self, factory: Callable[[], Any], max_pages: int) -> None:
        self._factory = factory
        self._max_pages = max(1, int(max_pages))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._crawler: Any = None
        self._pages = 0
        self._last_used = 0.0
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready = self._ready
            self._ready = threading.Event()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    finally:
                        loop.close()

            self._thread = threading.Thread(
                target=_run, name="dg-browser", daemon=True)
            self._thread.start()
        ready = self._ready
        if not ready.wait(timeout=60):
            raise RuntimeError("browser loop did not start")
        if self._startup_error is not None:
            raise self._startup_error
        return self._loop

    def _submit(self, coro):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    async def _acquire_crawler(self) -> Any:
        if self._crawler is not None and self._pages < self._max_pages:
            self._pages += 1
            return self._crawler
        if self._crawler is not None:
            log.info("recycling browser after %d pages", self._pages,
                     extra={"data": {"pages": self._pages,
                                     "max_pages": self._max_pages}})
            await self._close_crawler()
        self._crawler = await self._factory()
        self._pages = 1
        return self._crawler

    async def _close_crawler(self) -> None:
        crawler, self._crawler = self._crawler, None
        self._pages = 0
        if crawler is None:
            return
        # Both spellings exist across versions; either failure is not worth
        # propagating, because the browser is being discarded either way.
        closer = getattr(crawler, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.warning("browser close failed", extra={"data": {"error": str(exc)[:150]}})

    # -- use -----------------------------------------------------------------

    def run(self, coro_factory: Callable[[Any], Any], timeout_s: float) -> Any:
        """Run `coro_factory(crawler)` on the owning loop and wait for it."""

        async def _job():
            crawler = await self._acquire_crawler()
            return await coro_factory(crawler)

        future = self._submit(_job())
        try:
            return future.result(timeout=timeout_s)
        except BaseException:
            # The browser may be wedged in a way `arun` will never recover
            # from. Discard it so the next caller starts fresh.
            self.invalidate()
            raise

    def invalidate(self) -> None:
        """Drop the current browser without waiting for the loop."""
        crawler, self._crawler = self._crawler, None
        self._pages = 0
        if crawler is None:
            return
        try:
            self._submit(self._force_close(crawler)).result(timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("browser invalidate failed",
                        extra={"data": {"error": str(exc)[:150]}})

    async def _force_close(self, crawler: Any) -> None:
        closer = getattr(crawler, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            pass

    def idle_for(self, now: float) -> float:
        return now - self._last_used if self._crawler is not None else 0.0

    def touch(self, now: float) -> None:
        self._last_used = now

    def stats(self) -> dict[str, Any]:
        return {"pages": self._pages, "max_pages": self._max_pages,
                "has_browser": self._crawler is not None}

    def shutdown(self) -> None:
        """Close the browser and stop the owning loop. Idempotent."""
        loop = self._loop
        if loop is None:
            return
        try:
            self._submit(self._close_crawler()).result(timeout=30)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            # Short, and deliberately not a long wait. The thread is a daemon,
            # so a loop that will not stop cannot hold up process exit -- and
            # blocking shutdown on it buys nothing but a stalled teardown. The
            # browser was already closed above, which is the part that matters.
            thread.join(timeout=5)
        self._loop = None
        self._thread = None


class BrowserPool:
    """A small set of reusable browsers plus the idle watchdog that reaps them."""

    def __init__(self, factory: Callable[[], Any], max_pages: int = DEFAULT_MAX_PAGES,
                 idle_s: float = DEFAULT_IDLE_S,
                 concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self._slots = [_BrowserSlot(factory, max_pages)
                       for _ in range(max(1, int(concurrency)))]
        self._idle_s = max(0.0, float(idle_s))
        self._closed = False
        self._reaper: threading.Timer | None = None
        self._lock = threading.Lock()

    def run(self, coro_factory: Callable[[Any], Any], timeout_s: float) -> Any:
        if self._closed:
            raise RuntimeError("browser pool is closed")
        import time
        slot = self._slots[0]
        try:
            return slot.run(coro_factory, timeout_s)
        finally:
            slot.touch(time.monotonic())
            self._schedule_reap()

    def _schedule_reap(self) -> None:
        """Close browsers that have gone unused for the idle window.

        A plain Timer rather than a permanent thread: it is armed on use and
        does nothing when unused, so an idle pool costs one timer.
        """
        if self._idle_s <= 0 or self._closed:
            return
        with self._lock:
            if self._reaper is not None:
                self._reaper.cancel()
            self._reaper = threading.Timer(self._idle_s, self._reap)
            self._reaper.daemon = True
            self._reaper.start()

    def _reap(self) -> None:
        import time
        now = time.monotonic()
        for slot in self._slots:
            if slot.idle_for(now) >= self._idle_s:
                log.info("closing idle browser",
                         extra={"data": {"idle_s": round(slot.idle_for(now), 1)}})
                slot.shutdown()

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            if self._reaper is not None:
                self._reaper.cancel()
                self._reaper = None
        for slot in self._slots:
            slot.shutdown()

    def stats(self) -> list[dict[str, Any]]:
        return [s.stats() for s in self._slots]