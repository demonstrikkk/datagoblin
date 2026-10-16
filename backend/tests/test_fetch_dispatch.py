"""There must be exactly one fetch dispatch, and runs must go through it.

A byte-for-byte duplicate of the rung waterfall lived in `main._fetch_method`,
and runs used that copy while the per-rung timing was added to
`crawler._default_fetch`. Every real run therefore collected zero timing
samples, and nothing failed: the un-instrumented copy was correct code, so no
test complained and the report just quietly came back empty.

These tests exist so a second copy cannot reappear unnoticed.
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as main_mod  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402

RUNGS = ("crawl4ai", "impersonate", "jina", "docling")


def test_main_delegates_to_the_crawler_dispatch():
    """The live path must not reimplement the waterfall."""
    src = inspect.getsource(main_mod._fetch_method)
    for rung in RUNGS:
        assert f"fetcher.{rung}_fetch(" not in src, (
            f"main._fetch_method re-implements the {rung} rung; it must delegate "
            "to crawler._default_fetch or it bypasses instrumentation again")
    assert "impersonation_svc.impersonate_fetch(" not in src, \
        "main._fetch_method re-implements the impersonate rung"
    assert "crawler_svc._default_fetch" in src


def test_only_one_place_dispatches_on_the_rung():
    """A belt-and-braces check that the duplication has not come back."""
    offenders = []
    for path in Path(main_mod.__file__).parent.glob("**/*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Only the dispatcher itself should name crawl4ai_fetch as a branch.
        if "if method == \"crawl4ai\"" in text and "default_fetch" not in text:
            offenders.append(path.name)
    assert not offenders, (
        f"a second rung dispatch found in {offenders}; runs will bypass the "
        "instrumented one")


def test_the_timing_path_is_the_one_a_run_uses(monkeypatch):
    """End to end: dispatching through main records a timing sample."""
    crawler_svc.reset_fetch_timing()
    calls = []

    async def fake_http(url, *a, **k):
        calls.append(url)
        return {"url": url, "markdown": "x", "method": "http"}

    monkeypatch.setattr(fetcher, "http_fetch", fake_http)
    try:
        import asyncio
        asyncio.run(main_mod._fetch_method("https://s.example/", "http"))
        assert calls == ["https://s.example/"]
        timing = crawler_svc.fetch_timing()
        assert "http" in timing, (
            "a run's fetch recorded no timing; the instrumented dispatch is "
            "not the one runs use")
        assert timing["http"]["n"] == 1
    finally:
        crawler_svc.reset_fetch_timing()


@pytest.mark.parametrize("method", ["", "http", "crawl4ai", "jina", "docling"])
def test_delegation_still_reaches_each_rung(monkeypatch, method):
    """Delegation must not change which rung a request reaches."""
    seen = []

    def make(name):
        async def fn(url, *a, **k):
            seen.append(name)
            return {"url": url, "method": name}
        return fn

    monkeypatch.setattr(fetcher, "http_fetch", make("http"))
    monkeypatch.setattr(fetcher, "crawl4ai_fetch", make("crawl4ai"))
    monkeypatch.setattr(fetcher, "jina_fetch", make("jina"))
    monkeypatch.setattr(fetcher, "docling_fetch", make("docling"))

    import asyncio
    crawler_svc.reset_fetch_timing()
    try:
        asyncio.run(main_mod._fetch_method("https://s.example/", method))
        expected = "http" if method in ("", "http") else method
        assert seen == [expected], f"{method!r} reached {seen}"
    finally:
        crawler_svc.reset_fetch_timing()