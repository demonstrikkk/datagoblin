"""`RUN_RETRY_COUNT` has to count attempts, not rungs.

The waterfall loop was `for attempt, method in enumerate(methods)`, and
`RUN_RETRY_COUNT` was compared against `attempt` -- the position in the waterfall
rather than a retry counter. So:

  * `0 < 1` meant "this rung failed, go to the next one"
  * `1 < 1` meant "give up"
  * raising it to 3 changed nothing, because the loop had already ended

Every rung got exactly one shot, ever, and the setting that exists solely to
control that was inert. This file pins the knob so it cannot go inert again.

The case that made it matter is at the bottom: on the default `web:http` route
Chromium is the *last* rung, so it landed on `1 < 1` and was never retried. A
transient Playwright driver disconnect was therefore final for that page --
which is the class of failure behind the render losses recorded in
docs/39-REUSE-VERIFICATION.md, where the cause was recorded as unknown because
nobody had noticed the retry that was supposed to absorb it was never called.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import politeness as politeness_svc  # noqa: E402
from app.core.config import settings  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Politeness counters are process-global; the module docstring in
    test_phase3.py makes resetting them a house rule."""
    politeness_svc.reset_slots()
    politeness_svc.reset_stats()
    yield
    politeness_svc.reset_slots()
    politeness_svc.reset_stats()


@pytest.fixture()
def no_backoff(monkeypatch):
    """Real backoff sleeps 1-2s per retry. These tests are about counts."""
    slept: list = []

    async def _no_backoff(attempt):
        slept.append(attempt)

    from app.providers.crawl import fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "backoff", _no_backoff)
    return slept


def _retries(monkeypatch, n):
    monkeypatch.setattr(settings, "RUN_RETRY_COUNT", n)


def _ok(url, method):
    return {"url": url, "html": "<p>readable</p>" * 80, "method": method}


# --- the knob does what it says ----------------------------------------------

def test_one_is_one_attempt_per_rung(monkeypatch, no_backoff):
    """The default. `web:http` is http -> crawl4ai, so this is also the shape
    the shipped configuration actually runs."""
    _retries(monkeypatch, 1)
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "HTTP timeout", 502)
        return _ok(url, method)

    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                               asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai"
    assert calls == ["http", "crawl4ai"], "one attempt per rung at the default"


def test_raising_the_knob_actually_retries_the_same_rung(monkeypatch, no_backoff):
    """Before this was fixed, setting 3 here produced the identical call list."""
    _retries(monkeypatch, 3)
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "HTTP timeout", 502)
        return _ok(url, method)

    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai"
    assert calls == ["http", "http", "http", "crawl4ai"]
    # 2 waits between http's own attempts, then 1 before dropping to crawl4ai,
    # which succeeds first try and so waits for nothing.
    assert no_backoff == [0, 1, 2]


def test_the_backoff_grows_with_the_retry(monkeypatch):
    """A retry that fires immediately is not a retry, it is a re-roll."""
    _retries(monkeypatch, 3)
    slept: list = []

    async def _no_backoff(attempt):
        slept.append(attempt)

    from app.providers.crawl import fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "backoff", _no_backoff)

    async def _fetch(url, method):
        raise AppError("E_PROVIDER_TRANSIENT", "HTTP timeout", 502)

    with pytest.raises(AppError):
        run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                             asyncio.Semaphore(1)))
    # http: waits at 0 and 1 between its own attempts, then 2 before dropping.
    # crawl4ai: waits at 0 and 1, and nothing after -- it is the last rung, so
    # a pause before giving up on the page would only delay the error.
    assert slept == [0, 1, 2, 0, 1]


# --- the rung that actually mattered -----------------------------------------

def test_the_browser_rung_is_retried(monkeypatch, no_backoff):
    """Chromium is the LAST rung on `web:http`, so it landed on `1 < 1`.

    The old comparison therefore guaranteed zero retries and zero backoff for
    the one rung whose failures are transient by nature. This is the assertion
    that was impossible to write before, because the behaviour did not exist.
    """
    _retries(monkeypatch, 2)
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "HTTP timeout", 502)
        raise AppError("E_PROVIDER_TRANSIENT",
                       "Connection closed while reading from the driver", 502)

    with pytest.raises(AppError):
        run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                             asyncio.Semaphore(1)))
    assert calls == ["http", "http", "crawl4ai", "crawl4ai"]
    assert politeness_svc.stats.snapshot()["retry_reasons"] == {
        "http": 2, "crawl4ai": 2}


# --- what must NOT be retried -----------------------------------------------

def test_a_fatal_provider_error_is_never_retried(monkeypatch, no_backoff):
    """Retrying a provider that is simply absent just spends the page budget."""
    _retries(monkeypatch, 3)
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        raise AppError("E_PROVIDER_FATAL", "no such provider", 500)

    with pytest.raises(AppError):
        run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                             asyncio.Semaphore(1)))
    assert calls == ["http", "crawl4ai"], "one alternate attempt, no same-rung retry"


def test_a_shell_escalates_down_the_waterfall_instead_of_looping(monkeypatch, no_backoff):
    """Retrying a rung that returned an unreadable shell learns nothing.

    The escalation path is a `break` to the next rung rather than a `continue`,
    so raising the retry count does not turn a JS shell into three wasted
    attempts before the renderer is finally tried.
    """
    _retries(monkeypatch, 3)
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            # Under FETCH_THIN_CHARS, with a <script> tag: reads as a JS shell.
            return {"url": url, "html": "<html><script>boot()</script></html>",
                    "method": method}
        return _ok(url, method)

    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai"
    assert calls == ["http", "crawl4ai"], (
        "a shell must escalate, not be retried -- a second identical shell is "
        "not new information"
    )