"""A fetch that did not deliver must say so, per page, in the record.

The measured failure this exists to prevent, from docs/39:

    aws.amazon.com/transform/ rendered 17635 characters of content. The render
    then died. The waterfall fell back to the HTTP version. The dataset stored
    7885 characters of unrendered HTML -- 55% lost -- with `pages.method`
    reading `"http"`, no error, no event, no counter, and no field anywhere that
    recorded that a render had been attempted.

That is worse than an unrecorded failure, because the record did not merely omit
it. `_one` held the render exception in `last_err` and returned the HTTP page
without it, so the failure was *erased* -- the dataset asserted something it
could not support, and nothing anywhere contradicted it.

Six keys now describe every attempt, `source.failed` reports the ones that did
not deliver, and the taxonomy exists so "why do driver deaths happen" is
answerable from a count rather than from a 150-character string.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError, provider_transient  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _ok(markdown="Acme Corp was founded in 2011. " * 20):
    return {"url": "https://lists.example/acme", "html": "", "markdown": markdown,
            "content_hash": "h1", "status": "ok"}


def _shell():
    """Enough real text to be usable, but marked as needing a renderer.

    The escalation gate is `shell or hollow or sparse`. A page with plenty of
    clean text does not read as a `looks_js_shell` -- that test asks for *thin*
    text -- so a bare `<script>` tag does not escalate and the renderer is never
    tried. `has_hollow_code` does: a script plus an empty `<pre>`.

    The text has to exceed FETCH_THIN_CHARS as well, because the tail of the
    waterfall refuses to return anything shorter as a page at all. Both halves
    are what the measured case looked like: a page with real, usable text that
    still was not what had been asked for.
    """
    body = "Acme Corp was founded in 2011 by Jane Roe. " * 25
    return {"url": "https://lists.example/acme",
            "markdown": body,
            "html": ("<html><body><script>render()</script>"
                     f"<p>{body}</p><pre></pre></body></html>"),
            "content_hash": "h0", "status": "ok"}


DISCONNECT = "Connection closed while reading from the driver"


def _degrading_fetch(calls=None):
    """HTTP answers with a shell, the renderer dies."""
    async def _fetch(url, method):
        if calls is not None:
            calls.append(method)
        if method == "http":
            return _shell()
        raise provider_transient(DISCONNECT)
    return _fetch


# --- the failure was erased, not merely unrecorded ---------------------------

def test_a_dead_render_still_reports_that_it_dead():
    """The load-bearing assertion.

    HTTP answers with real text, so `best` is set and the page is returned
    rather than failed -- which is the right call, the HTTP text is better than
    nothing. What it must not do is return that page as if the render had
    worked.
    """
    async def _fetch(url, method):
        if method == "http":
            return _ok()
        raise provider_transient(DISCONNECT)

    page = run(crawler_svc._one("https://lists.example/acme", "web:http",
                                _degrading_fetch(), asyncio.Semaphore(1)))
    assert page["render_attempted"] is True
    assert page["render_status"] == "failed"
    assert page["render_error_code"] == "browser_disconnected"
    assert page["fallback_used"] is True
    assert page["final_content_source"] == "http"
    assert DISCONNECT[:40] in page["degraded_reason"], (
        "the reason the page is not what was asked for has to travel with it")


def test_the_error_code_is_a_closed_taxonomy_member():
    page_codes = set(fetcher.RENDER_ERROR_CODES)
    assert page_codes, "the taxonomy is empty"
    assert "unknown" in page_codes, (
        "a failure that cannot be classified is a finding; without this member "
        "the counts would hide it")
    for value in ("browser_disconnected", "browser_crash", "navigation_timeout",
                  "browser_launch_failed", "content_extraction_error"):
        assert value in page_codes


def test_a_clean_http_page_is_not_degraded():
    """Otherwise every ordinary page is flagged and the flag means nothing."""
    async def _fetch(url, method):
        return _ok()

    page = run(crawler_svc._one("https://lists.example/acme", "web:http",
                                _fetch, asyncio.Semaphore(1)))
    assert page["render_attempted"] is False
    assert page["render_status"] == "not_needed"
    assert page["render_error_code"] == ""
    assert page["fallback_used"] is False
    assert page["final_content_source"] == "http"


def test_a_successful_render_is_distinguishable_from_no_render():
    """`not_needed` and `succeeded` are different facts.

    A page that never needed a renderer proves nothing about whether the
    renderer works, and a run's render success rate is the number that was
    unanswerable.
    """
    async def _fetch(url, method):
        if method == "http":
            # Thin enough with a script tag to escalate to the renderer.
            return {"url": url, "html": "<html><script>x()</script></html>",
                    "content_hash": "h0", "status": "ok"}
        return _ok()

    page = run(crawler_svc._one("https://lists.example/acme", "web:http",
                                _fetch, asyncio.Semaphore(1)))
    assert page["render_attempted"] is True
    assert page["render_status"] == "succeeded"
    assert page["final_content_source"] == "rendered"
    assert page["fallback_used"] is False


def test_every_rung_tried_is_recorded_in_order():
    page = run(crawler_svc._one("https://lists.example/acme", "web:http",
                                _degrading_fetch(), asyncio.Semaphore(1)))
    attempts = page["fetch_attempts"]
    assert [a["method"] for a in attempts] == ["http", "crawl4ai"]
    assert [a["outcome"] for a in attempts] == ["ok", "failed"]
    assert attempts[1]["error_code"] == "browser_disconnected"


def test_a_retry_appears_as_a_separate_attempt():
    """Two tries are two attempts. Collapsing them hides the flakiness."""
    calls = {"n": 0}

    async def _fetch(url, method):
        if method == "http":
            return _shell()
        calls["n"] += 1
        if calls["n"] < 2:
            raise provider_transient(DISCONNECT)
        return _ok()

    from app.core.config import settings as _s
    import app.providers.crawl.fetcher as f
    real = _s.RUN_RETRY_COUNT
    try:
        _s.RUN_RETRY_COUNT = 3
        page = run(crawler_svc._one("https://lists.example/acme", "web:http",
                                    _fetch, asyncio.Semaphore(1)))
    finally:
        _s.RUN_RETRY_COUNT = real
    assert page["render_status"] == "succeeded"
    assert sum(1 for a in page["fetch_attempts"] if a["method"] == "crawl4ai") == 2


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("Connection closed while reading from the driver", "browser_disconnected"),
    ("Target page, context or browser has been closed", "browser_disconnected"),
    ("WebSocket closed", "browser_disconnected"),
    ("Page crashed!", "browser_crash"),
    ("Renderer process crashed, out of memory", "browser_crash"),
    ("Timeout 90000ms exceeded", "navigation_timeout"),
    ("executable doesn't exist at /usr/bin/chromium", "browser_launch_failed"),
    ("something nobody has seen before", "unknown"),
    ("", "unknown"),
])
def test_render_errors_are_named_not_described(message, expected):
    """Disconnection before crash, deliberately.

    They have opposite remedies -- wait and retry versus restart the browser --
    so getting the order backwards would send the next investigation the wrong
    way, and both words appear in the wild in overlapping messages.
    """
    assert fetcher.classify_render_error(message) == expected


def test_classification_never_guesses():
    """Fails safe to `unknown`, never to a specific code."""
    assert fetcher.classify_render_error(None) == "unknown"
    assert fetcher.classify_render_error(object()) == "unknown"
    assert fetcher.classify_render_error(12345) == "unknown"


def test_the_code_the_fetch_layer_chose_is_not_rederived():
    """It was computed where the real exception was visible.

    Re-deriving it from a 150-character message would throw away the only place
    with the full text.
    """
    err = provider_transient("truncated...", {"render_error_code": "browser_crash"})
    assert crawler_svc._render_code(err, "crawl4ai") == "browser_crash"


def test_a_non_render_rung_is_not_mislabelled_as_a_render_failure():
    """`http_error` and `browser_crash` have different causes.

    Merging them would make the counts unreadable, which is the whole reason for
    the taxonomy.
    """
    err = provider_transient("connection reset by peer")
    assert crawler_svc._render_code(err, "http") == "not_applicable"


def test_a_timeout_is_distinguished_from_a_death():
    assert crawler_svc._status_for_error(asyncio.TimeoutError()) == "timed_out"
    assert crawler_svc._status_for_error(provider_transient("boom")) == "failed"


# --- the event ----------------------------------------------------------------

SEED = [{"url": "https://lists.example/acme", "title": ""}]
PLAN = {"max_pages": 10, "max_depth": 0, "max_domain_pages": 5,
        "children_per_page": 0, "seed_budget_share": 1.0}


def _crawl(fetch, *, persist_page=None, events=None, sources=None):
    """`fetch_all` through its real signature.

    Positional, as `test_crawl_resilience` calls it, because the keyword names are
    not `store`/`run_id`/`seeds`/`notify`/`budget` and guessing them produced a
    call that failed on argument binding rather than on anything meaningful.
    """
    async def _emit(ev):
        if events is not None:
            events.append(ev)

    def _persist_source(run_id, source):
        if sources is not None:
            sources.append(source)

    return run(crawler_svc.fetch_all(
        SEED, fetch, _emit, _persist_source, PLAN, persist_page, None))


def test_a_degraded_fetch_is_announced_distinctly_from_a_clean_one():
    """It used to emit nothing at all.

    `fetch_all` recorded the source row and returned, so the run's history said
    a page had been fetched whenever one had been and was silent about every one
    that had not -- including the case that mattered most, where a page was
    stored but was not what was asked for.
    """
    events: list[dict] = []
    sources: list[dict] = []

    async def _persist(page):
        return "p1"

    _crawl(_degrading_fetch(), persist_page=_persist, events=events,
           sources=sources)

    by_type = {e["type"]: e for e in events}
    assert "source.fetched" in by_type, "the page was stored, so it was fetched"
    assert "source.failed" in by_type, (
        "a page served over HTTP after a render died emitted nothing")
    degraded = by_type["source.failed"]
    assert degraded["data"]["status"] == "degraded"
    assert degraded["data"]["error_code"] == "browser_disconnected"
    assert by_type["source.fetched"]["data"]["ok"] is True, (
        "a degraded page is stored and usable; it is not a failed fetch")


def test_the_degraded_event_names_the_cause():
    ev = crawler_svc._source_failed_event(
        "https://x.example/a", "Connection closed", error_code="browser_disconnected",
        fallback_used=True)
    assert ev["type"] == "source.failed"
    assert ev["data"]["status"] == "degraded"
    assert ev["data"]["error_code"] == "browser_disconnected"
    assert ev["data"]["fallback_used"] is True
    assert ev["data"]["ok"] is False
    assert "Degraded" in ev["message"]


def test_the_three_statuses_are_distinguishable():
    """A degraded page is stored. A failed one is not. A UI must not merge them."""
    statuses = {
        crawler_svc._source_failed_event("u", "r")["data"]["status"],
        crawler_svc._source_failed_event("u", "r", status="skipped")["data"]["status"],
        crawler_svc._source_failed_event("u", "r", fallback_used=True)["data"]["status"],
    }
    assert statuses == {"failed", "skipped", "degraded"}


# --- durability ---------------------------------------------------------------

def test_the_outcome_reaches_the_stored_row():
    """Recorded on the page dict and handed to `persist_page`.

    The page row is the evidence record, so a degraded fetch that is only
    announced on the event stream and then forgotten is not fixed.
    """
    captured: list[dict] = []

    async def _persist(page):
        captured.append(page)
        return "p1"

    _crawl(_degrading_fetch(), persist_page=_persist)
    assert captured, "nothing was persisted, so the test proves nothing"
    row = captured[0]
    assert row["render_status"] == "failed"
    assert row["render_error_code"] == "browser_disconnected"
    assert row["fallback_used"] is True
    assert row["final_content_source"] == "http"
    assert row["fetch_attempts"], "the attempt history must be stored, not just reported"


def test_the_migration_adds_every_column_the_writer_uses():
    mig = (Path(__file__).resolve().parents[1] / "migrations"
           / "010_fetch_outcome.sql").read_text(encoding="utf-8")
    for col in ("fetch_attempts", "render_attempted", "render_status",
                "render_error_code", "fallback_used", "final_content_source",
                "degraded_reason"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in mig, f"{col} is written but not stored"
    # `pages` records only successes, so a failure needs somewhere else to land.
    for col in ("render_status", "render_error_code", "fallback_used"):
        assert f"sources\n    ADD COLUMN IF NOT EXISTS {col}" in mig or \
            f"ALTER TABLE sources" in mig, f"sources.{col} is written but not stored"