"""Tests for per-rung fetch timing.

The instrumentation's value depends entirely on two things being true that are
easy to get wrong:

  - a rung that returned a skip or an error must NOT be counted as a success,
    because that makes the timing look better than the crawl actually was;
  - the report must show p50/p90 rather than only a mean, because the failure
    this exists to diagnose is "usually fast, occasionally terrible", and a mean
    hides exactly that.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import crawler as crawler_svc  # noqa: E402


@pytest.fixture(autouse=True)
def _clear():
    crawler_svc.reset_fetch_timing()
    yield
    crawler_svc.reset_fetch_timing()


def _patch(monkeypatch, **results):
    """Replace each rung with a canned result, recording call order."""
    seen = []

    def make(name, out):
        async def fn(url, *a, **k):
            seen.append(name)
            if isinstance(out, BaseException):
                raise out
            return out
        return fn

    from app.providers.crawl import fetcher
    monkeypatch.setattr(fetcher, "http_fetch", make("http", results.get("http", {"method": "http"})))
    monkeypatch.setattr(fetcher, "crawl4ai_fetch", make("crawl4ai", results.get("crawl4ai", {"method": "crawl4ai"})))
    monkeypatch.setattr(fetcher, "jina_fetch", make("jina", results.get("jina", {"method": "jina"})))
    monkeypatch.setattr(fetcher, "docling_fetch", make("docling", results.get("docling", {"method": "docling"})))
    return seen


# -- dispatch still routes correctly -------------------------------------------

@pytest.mark.parametrize("method,rung", [
    ("", "http"), ("http", "http"), ("crawl4ai", "crawl4ai"),
    ("jina", "jina"), ("docling", "docling"),
])
def test_each_method_still_reaches_its_own_rung(monkeypatch, method, rung):
    seen = _patch(monkeypatch)
    asyncio.run(crawler_svc._default_fetch("https://s.example/", method))
    assert seen == [rung], f"{method!r} reached {seen}, expected {rung}"


# -- the thing that would make this lie ----------------------------------------

def test_a_skipped_fetch_is_not_counted_as_success(monkeypatch):
    _patch(monkeypatch, http={"url": "u", "skipped": "robots-disallowed",
                              "method": "http"})
    out = asyncio.run(crawler_svc._default_fetch("https://s.example/", "http"))
    assert out["skipped"] == "robots-disallowed", "the payload must be returned as-is"

    rung = crawler_svc.fetch_timing()["http"]
    assert rung["failed"] == 1, "a skip was counted as a successful fetch"
    assert rung["outcomes"] == {"robots-disallowed": 1}, \
        "the skip reason must be named, not just counted"


def test_an_error_payload_is_not_counted_as_success(monkeypatch):
    _patch(monkeypatch, http={"url": "u", "error": "boom", "method": "http"})
    asyncio.run(crawler_svc._default_fetch("https://s.example/", "http"))
    assert crawler_svc.fetch_timing()["http"]["failed"] == 1


def test_a_raised_error_is_recorded_and_re_raised(monkeypatch):
    _patch(monkeypatch, http=RuntimeError("timeout"))
    with pytest.raises(RuntimeError):
        asyncio.run(crawler_svc._default_fetch("https://s.example/", "http"))

    rung = crawler_svc.fetch_timing()["http"]
    assert rung["failed"] == 1
    assert "RuntimeError" in str(rung["outcomes"]), \
        "a raised error must be distinguishable from a returned skip"


def test_a_successful_fetch_is_counted(monkeypatch):
    _patch(monkeypatch, http={"url": "u", "markdown": "x", "method": "http"})
    asyncio.run(crawler_svc._default_fetch("https://s.example/", "http"))
    assert crawler_svc.fetch_timing()["http"]["failed"] == 0


# -- distribution, not just a mean ---------------------------------------------

def test_the_report_shows_p90_and_not_only_a_mean():
    """The 'usually fast, occasionally terrible' case is invisible to a mean.

    Ten slow samples in a hundred, so p90 genuinely lands on them. Note this is
    the threshold: with a *single* outlier in a hundred, p90 correctly stays in
    the fast bulk and only max reveals it. Percentiles show you the shape, they
    do not promise to catch every rare event.
    """
    for _ in range(90):
        crawler_svc._note_timing("crawl4ai", 100, True, "ok")
    for _ in range(10):
        crawler_svc._note_timing("crawl4ai", 30000, True, "ok")

    r = crawler_svc.fetch_timing()["crawl4ai"]
    assert r["p50_ms"] <= 200, "p50 should sit in the fast bulk"
    assert r["p90_ms"] >= 30000, \
        "p90 missed the slow tail; a mean-only report hides this entirely"
    assert r["max_ms"] == 30000
    # The mean sits between the two clusters and describes neither.
    mean = r["total_ms"] / r["n"]
    assert 100 < mean < 30000, "a mean-only report would hide both clusters"


def test_a_rare_outlier_is_still_visible_through_max():
    """One bad sample in a hundred: p90 stays clean, max must not."""
    for _ in range(99):
        crawler_svc._note_timing("crawl4ai", 100, True, "ok")
    crawler_svc._note_timing("crawl4ai", 30000, True, "ok")
    r = crawler_svc.fetch_timing()["crawl4ai"]
    assert r["max_ms"] == 30000


def test_sample_counts_and_totals_are_consistent():
    for ms in (10, 20, 30, 40):
        crawler_svc._note_timing("http", ms, True, "ok")
    r = crawler_svc.fetch_timing()["http"]
    assert r["n"] == 4
    assert r["total_ms"] == 100
    assert r["min_ms"] == 10 and r["max_ms"] == 40


def test_a_single_sample_does_not_break_the_percentiles():
    crawler_svc._note_timing("http", 42, True, "ok")
    r = crawler_svc.fetch_timing()["http"]
    assert r["p50_ms"] == 42 and r["p90_ms"] == 42


def test_an_empty_report_is_empty_not_an_error():
    assert crawler_svc.fetch_timing() == {}


def test_outcomes_are_ordered_most_frequent_first():
    for _ in range(5):
        crawler_svc._note_timing("http", 1, False, "robots-disallowed")
    crawler_svc._note_timing("http", 1, False, "timeout")
    outcomes = crawler_svc.fetch_timing()["http"]["outcomes"]
    assert list(outcomes) == ["robots-disallowed", "timeout"]


# -- bounded, and shared safely ------------------------------------------------

def test_the_window_is_bounded():
    from app.services.crawler import _TIMING_WINDOW
    for i in range(_TIMING_WINDOW + 150):
        crawler_svc._note_timing("http", i, True, "ok")
    r = crawler_svc.fetch_timing()["http"]
    assert r["n"] == _TIMING_WINDOW, "an unbounded window is a memory leak"

    # Trimming must keep the most recent samples, not the first ones.
    assert r["min_ms"] == 150, "the window trimmed the wrong end"


def test_the_window_keeps_the_most_recent_samples():
    from app.services.crawler import _TIMING_WINDOW
    for i in range(_TIMING_WINDOW + 10):
        crawler_svc._note_timing("http", i, True, "ok")
    assert crawler_svc.fetch_timing()["http"]["max_ms"] == _TIMING_WINDOW + 9


def test_concurrent_notes_do_not_corrupt_the_report():
    """Six writers, no lost samples.

    Stays under the window deliberately: a run that exceeds it would be trimmed
    by design, which would mask exactly the loss this is checking for.
    """
    import threading
    from app.services.crawler import _TIMING_WINDOW

    per_thread = _TIMING_WINDOW // 8   # 25 each, 150 total, under the window
    total = per_thread * 8

    def worker(base):
        for i in range(per_thread):
            crawler_svc._note_timing("http", base + i, True, "ok")

    threads = [threading.Thread(target=worker, args=(n * 1000,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    r = crawler_svc.fetch_timing()["http"]
    assert r["n"] == total, f"{total - r['n']} samples lost to concurrent writes"
    # Every sample has a distinct ms, so the max proves none were overwritten.
    assert r["max_ms"] == 7 * 1000 + per_thread - 1


def test_reset_clears_every_rung():
    crawler_svc._note_timing("http", 1, True, "ok")
    crawler_svc._note_timing("crawl4ai", 1, True, "ok")
    crawler_svc.reset_fetch_timing()
    assert crawler_svc.fetch_timing() == {}


def test_the_snapshot_does_not_expose_mutable_internal_state():
    crawler_svc._note_timing("http", 5, True, "ok")
    first = crawler_svc.fetch_timing()
    first["http"]["n"] = 999
    assert crawler_svc.fetch_timing()["http"]["n"] == 1, \
        "the report handed out a live reference into the sample store"