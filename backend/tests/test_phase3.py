"""Phase-3 tests: politeness math, traversal gating, streaming export, pipeline.

Hermetic: no network. Politeness singletons (slots/stats) are reset per test.
"""
import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import exporter as exporter_svc  # noqa: E402
from app.services import item_pipeline as pipeline_svc  # noqa: E402
from app.services import politeness as politeness_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_politeness():
    politeness_svc.reset_slots()
    politeness_svc.reset_stats()
    yield
    politeness_svc.reset_slots()
    politeness_svc.reset_stats()


# -- normalize_url ---------------------------------------------------------------
def test_normalize_url_canonical():
    n = politeness_svc.normalize_url
    assert n("HTTP://Example.COM:80/a/?b=2&a=1&utm_source=x#frag") == \
        "http://example.com/a/?a=1&b=2"
    assert n("https://example.com:8443/x") == "https://example.com:8443/x"
    assert n("https://example.com/a", keep_fragments=False) == "https://example.com/a"
    assert n("https://example.com/a#s", keep_fragments=True) == "https://example.com/a#s"
    assert n("not a url") == "not a url"
    assert n("") == ""


def test_fingerprint_stable_and_sensitive():
    f = politeness_svc.fingerprint
    a = f("GET", "https://example.com/a?b=2&a=1&utm_x=1")
    assert a == f("get", "HTTPS://EXAMPLE.COM:443/a?a=1&b=2")
    assert a != f("POST", "https://example.com/a?a=1&b=2")
    assert a != f("GET", "https://example.com/a?a=1&b=2", body=b"{}")
    assert len(a) == 40  # sha1 hex


# -- Retry-After -------------------------------------------------------------------
def test_parse_retry_after():
    p = politeness_svc.parse_retry_after
    assert p("120") == 120.0
    assert p(None) == 0.0 and p("") == 0.0 and p("garbage") == 0.0
    assert p("99999") == 300.0  # capped
    assert p("Wed, 21 Oct 2115 07:28:00 GMT") > 0.0  # future HTTP-date
    assert p("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # past date


# -- AutoThrottle --------------------------------------------------------------------
def test_delay_unseen_host_zero():
    assert politeness_svc.delay_for("new.example") == 0.0


def test_record_converges_and_freezes():
    host = "h.example"
    d1 = politeness_svc.record(host, 2.0, True)   # target 2.0 -> (0+2)/2=1, max(2,1)=2
    assert d1 == 2.0
    d2 = politeness_svc.record(host, 2.0, True)   # (2+2)/2=2
    assert d2 == 2.0
    d3 = politeness_svc.record(host, 0.1, False)  # trouble: (2+0.1)/2=1.05 < 2 -> freeze
    assert d3 == 2.0
    d4 = politeness_svc.record(host, 30.0, False)  # (2+30)/2=16 -> max(30,16)=30 applies
    assert d4 == 30.0
    d5 = politeness_svc.record(host, 0.0, True, retry_after=5.0)  # penalty floor
    assert d5 >= 5.0
    d6 = politeness_svc.record(host, 1000.0, True)
    assert d6 == 60.0  # capped


def test_strategy_buckets():
    s = politeness_svc.strategy
    assert s(AppError("E_PROVIDER_TRANSIENT", "SSL: tls handshake failure", 502)) == "escalate"
    assert s(AppError("E_PROVIDER_TRANSIENT", "HTTP 406 not acceptable", 502)) == "escalate"
    assert s(AppError("E_PROVIDER_TRANSIENT", "HTTP timeout x", 502)) == "retry"
    assert s(RuntimeError("boom")) == "retry"
    assert politeness_svc.is_fatal_status(403) and politeness_svc.is_retry_status(503)
    assert not politeness_svc.is_fatal_status(200)


def test_observe_updates_slot_and_stats():
    politeness_svc.observe("h.example", 200, 1.0, 500)
    politeness_svc.observe("h.example", 403, 0.5)
    snap = politeness_svc.stats.snapshot()
    assert snap["status_codes"] == {200: 1, 403: 1}
    assert snap["blocked"] == 1
    assert snap["hosts"]["h.example"] == {"requests": 2, "bytes": 500, "errors": 1}
    assert "h.example" in snap["delays"]


# -- fetcher error taxonomy (Phase-6 except-fix) -----------------------------------------
def _patch_fetch_guards(monkeypatch):
    from app.providers.crawl import fetcher as fetcher_mod

    async def _aguard(url):
        return url

    async def _robots(url):
        return True, 0.0

    async def _throttle(host, floor=0.0):
        return None

    monkeypatch.setattr(fetcher_mod, "aguard_url", _aguard)
    monkeypatch.setattr(fetcher_mod, "robots_allowed", _robots)
    monkeypatch.setattr(fetcher_mod, "throttle", _throttle)
    return fetcher_mod


def test_http_ssrf_redirect_raises_app_error_not_type_error(monkeypatch):
    from types import SimpleNamespace
    from app.core.errors import AppError
    fetcher_mod = _patch_fetch_guards(monkeypatch)

    class _Resp:
        history = [SimpleNamespace(url="http://127.0.0.1/x")]
        url = "http://127.0.0.1/x"
        headers = {}
        status_code = 200

        async def aread(self):
            return b""

        def raise_for_status(self):
            pass

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(fetcher_mod, "_client_pool", lambda: _Client())
    with pytest.raises(AppError) as ei:
        run(fetcher_mod.http_fetch("https://x.example/a"))
    assert ei.value.code == "E_PROVIDER_FATAL"


def test_crawl4ai_failure_raises_app_error(monkeypatch):
    from app.core.errors import AppError
    fetcher_mod = _patch_fetch_guards(monkeypatch)

    def _boom(url, timeout_s):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(fetcher_mod, "_sync_crawl", _boom)
    with pytest.raises(AppError) as ei:
        run(fetcher_mod.crawl4ai_fetch("https://x.example/a"))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


# -- thinness escalation -----------------------------------------------------------------
def test_looks_js_shell():
    f = politeness_svc.looks_js_shell
    assert f("<html><script src='a.js'></script><div id=root></div></html>", 29, 500)
    assert not f("<html><body><p>short but complete</p></body></html>", 113, 500)
    assert not f("<html><script src='a.js'></script></html>", 600, 500)  # rich enough
    assert not f("", 0, 500)


def test_has_hollow_code():
    f = politeness_svc.has_hollow_code
    assert f("<html><script src='h.js'></script><pre></pre><p>rich prose here</p></html>")
    assert f("<html><script>var x=1;</script><code>   </code></html>")
    assert not f("<html><pre>SELECT 1</pre><p>prose</p></html>")  # filled code: no signal
    assert not f("<html><pre></pre><p>prose</p></html>")  # no scripts: static page
    assert not f("")


def test_low_density_shell():
    f = politeness_svc.low_density_shell
    big = "<html><head><script src='a.js'></script></head><body><p>x</p></body></html>"
    assert f(big * 500, 1000, len(big) * 500)  # 65KB shell, 1KB text
    assert not f(big * 500, 10000, len(big) * 500)  # 15% density: real content
    assert not f("<p>small</p>", 100, 500)  # small page, not a shell
    assert not f(big * 500, 1000, len(big) * 500 - 60000)  # guard: html_len<=20000
    noscript = "<html><body>" + ("<p>word </p>" * 300) + "</body></html>"
    assert not f(noscript * 10, 1000, len(noscript) * 10)  # no scripts: no signal


def test_low_density_escalates_to_renderer():
    calls: list = []
    prose = "<p>" + "Word " * 250 + "</p>"  # ~1250 chars of real text
    padding = "<script>/* " + "x" * 30000 + " */</script>"  # shell ballast
    html = f"<html><head>{padding}</head><body><nav>menu</nav>{prose}</body></html>"

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            return {"url": url, "method": "http", "html": html}
        return {"url": url, "method": "crawl4ai",
                "markdown": "# Full article\n\n" + "Rendered words. " * 200}

    page = run(crawler_svc._one("https://x.example/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai" and calls == ["http", "crawl4ai"]


def test_thin_shell_escalates_to_renderer():
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            return {"url": url, "method": "http",
                    "html": "<html><head><script src='app.js'></script></head>"
                            "<body><div id='root'></div></body></html>",
                    "text": "x"}
        return {"url": url, "method": "crawl4ai",
                "markdown": "# Real content\n\n" + "Rendered body text. " * 40}

    page = run(crawler_svc._one("https://x.example/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai" and calls == ["http", "crawl4ai"]
    assert politeness_svc.stats.snapshot()["retry_reasons"] == {"thin-escalate:http": 1}


def test_scriptless_thin_returns_at_once():
    calls: list = []

    async def _fetch(url, method):
        calls.append(method)
        return {"url": url, "method": method, "html": "<html><body><p>tiny</p></body></html>"}

    page = run(crawler_svc._one("https://x.example/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "http" and calls == ["http"]


def test_hollow_code_escalates_despite_rich_prose():
    calls: list = []
    prose = "<p>" + "Rich tutorial prose. " * 60 + "</p>"

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            return {"url": url, "method": "http",
                    "html": f"<html><head><script src='hl.js'></script></head>"
                            f"<body>{prose}<pre>   </pre></body></html>"}
        return {"url": url, "method": "crawl4ai",
                "markdown": "# Tutorial\n\nProse here.\n\n```sql\nSELECT 1\n```"}

    page = run(crawler_svc._one("https://x.example/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai" and calls == ["http", "crawl4ai"]


def test_all_thin_returns_longest():
    async def _fetch(url, method):
        if method == "http":
            return {"url": url, "method": "http",
                    "html": "<script src='a.js'></script><p>short</p>"}
        return {"url": url, "method": "crawl4ai",
                "markdown": "a bit longer rendered text here yes indeed"}

    page = run(crawler_svc._one("https://x.example/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai"  # longest thin page wins


# -- traversal gating ------------------------------------------------------------------
def test_traversal_allowed_extensions_and_domains():
    t = crawler_svc.traversal_allowed
    assert t("https://x.example/a/b", {})
    assert not t("https://x.example/a/file.pdf", {})
    assert not t("https://x.example/a/archive.zip", {})
    assert not t("https://x.example/a/img.png", {})
    assert t("https://x.example/feed", {})  # feeds/sitemaps stay traversable
    plan = {"seed_domains": ["x.example"], "allowed_sources": ["https://y.example/s"]}
    assert t("https://x.example/a", plan)
    assert t("https://sub.y.example/b", plan)
    assert not t("https://other.example/c", plan)


def test_pick_index_round_robin():
    q = [({"url": "https://a.example/1"}, 0), ({"url": "https://a.example/2"}, 0),
         ({"url": "https://b.example/1"}, 0)]
    assert crawler_svc._pick_index(q, {"a.example": 2}) == 2  # least-settled host
    assert crawler_svc._pick_index(q, {}) == 0  # tie -> earliest


def test_escalate_skips_backoff(monkeypatch):
    from app.providers.crawl import fetcher as fetcher_mod
    calls: list = []
    slept: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "SSL: tls failure", 502)
        return {"url": url, "html": "<p>x</p>", "method": method}

    async def _no_backoff(attempt):
        slept.append(attempt)

    monkeypatch.setattr(fetcher_mod, "backoff", _no_backoff)
    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai" and calls == ["http", "crawl4ai"]
    assert slept == []  # escalate: alternate immediately
    assert politeness_svc.stats.snapshot()["retry_reasons"] == {"escalate:http": 1}


def test_retry_notes_and_backs_off(monkeypatch):
    from app.providers.crawl import fetcher as fetcher_mod
    calls: list = []
    slept: list = []

    async def _fetch(url, method):
        calls.append(method)
        if method == "http":
            raise AppError("E_PROVIDER_TRANSIENT", "HTTP timeout x", 502)
        return {"url": url, "html": "<p>x</p>", "method": method}

    async def _no_backoff(attempt):
        slept.append(attempt)

    monkeypatch.setattr(fetcher_mod, "backoff", _no_backoff)
    page = run(crawler_svc._one("https://example.com/a", "web:http", _fetch,
                                asyncio.Semaphore(1)))
    assert page["method"] == "crawl4ai" and slept == [0]
    assert politeness_svc.stats.snapshot()["retry_reasons"] == {"http": 1}


# -- streaming export ---------------------------------------------------------------------
def _rows():
    return [{"fields": {"company_name": {
        "value": "Acme=1", "verification_status": "verified",
        "source": {"url": "u", "title": "t", "quote": "q", "retrieved_at": "now"}}}}]


def test_csv_stream_matches_buffered():
    schema = [{"name": "company_name"}]
    buf = io.StringIO(newline="")
    w = exporter_svc.CsvStreamWriter(buf)
    w.start(schema)
    for r in _rows():
        w.write_row(r)
    assert w.finish() == 1
    assert buf.getvalue() == exporter_svc.to_csv(schema, _rows())


def test_csv_stream_lifecycle_guards():
    buf = io.StringIO(newline="")
    w = exporter_svc.CsvStreamWriter(buf)
    with pytest.raises(ValueError):
        w.write_row({})
    w.start([])
    with pytest.raises(ValueError):
        w.start([])
    w2 = exporter_svc.CsvStreamWriter(buf, max_rows=1)
    w2.start([{"name": "a"}])
    w2.write_row({"fields": {}})
    w2.write_row({"fields": {}})  # over cap: ignored
    assert w2.finish() == 1


def test_jsonl_roundtrip_and_cap():
    rows = _rows() * 3
    lines = exporter_svc.to_jsonl(rows).splitlines()
    assert len(lines) == 3 and all(json.loads(line) for line in lines)
    buf = io.StringIO()
    w = exporter_svc.JsonLinesWriter(buf, max_rows=2)
    w.start()
    for r in rows:
        w.write_row(r)
    assert w.finish() == 2 and len(buf.getvalue().splitlines()) == 2
    with pytest.raises(ValueError):
        exporter_svc.JsonLinesWriter(buf).write_row({})


def test_columns_pin_and_jsonl_format():
    schema = [{"name": "company_name"}, {"name": "founder"}]
    rows = [{"fields": {"company_name": {"value": "A", "verification_status": "verified",
                                         "source": {}},
                        "founder": {"value": "B", "verification_status": "verified",
                                    "source": {}}}}]
    pinned = exporter_svc.to_csv(schema, rows, columns=["founder"])
    assert pinned.splitlines()[0].startswith("founder,")
    assert ",A," not in pinned
    fmt, content, name = exporter_svc.export_dataset(schema, rows, "jsonl")
    assert (fmt, name) == ("jsonl", "dataset.jsonl") and len(content.splitlines()) == 1


def _report_rows():
    return [{"fields": {
        "company_name": {"value": "Acme", "verification_status": "verified",
                         "source": {"url": "https://x.example/a", "title": "T",
                                    "quote": "Acme valued at 100 crore",
                                    "retrieved_at": "2026-09-26T00:00:00Z"}},
        "location": {"value": "Bengaluru", "verification_status": "needs_review",
                     "source": {"url": "https://x.example/b", "title": "",
                                "quote": "", "retrieved_at": ""}}}},
        {"fields": {"company_name": "Beta"}}]  # scalar cell form


def test_markdown_report_renders_evidence():
    schema = [{"name": "company_name"}, {"name": "location"}]
    meta = {"name": "Q", "run_id": "r1",
            "counts": {"attempted": 2, "successful": 2, "failed": 0, "skipped": 0,
                       "verified": 1, "needs_review": 1}}
    md = exporter_svc.to_markdown_report(schema, _report_rows(), meta)
    assert md.startswith("# Q") and "Acme" in md and "verified" in md
    assert "https://x.example/a" in md and "100 crore" in md  # source + quote
    assert "## Record 2" in md and "Beta" in md
    fmt, content, name = exporter_svc.export_dataset(schema, _report_rows(), "md",
                                                     meta=meta)
    assert (fmt, name) == ("md", "report.md") and content == md


def test_markdown_report_empty_is_honest():
    md = exporter_svc.to_markdown_report([{"name": "company_name"}], [],
                                         {"name": "Q", "run_id": "r"})
    assert "Records: **0**" in md and "honest empty" in md
    fmt, _, _ = exporter_svc.export_dataset([{"name": "a"}], [], "report")
    assert fmt == "md"  # empty report renders, never raises


# -- item pipeline ----------------------------------------------------------------------------
def _stage_funcs(events):
    async def _up(item):
        if item["v"] == 2:
            raise pipeline_svc.DropItem("bad value")
        return {"v": item["v"] + 1}

    async def _open_up():
        events.append("open:up")

    async def _close_up():
        events.append("close:up")

    async def _dbl(item):
        return {"v": item["v"] * 2}

    async def _open_dbl():
        events.append("open:dbl")

    async def _close_dbl():
        events.append("close:dbl")

    _up.open, _up.close, _dbl.open, _dbl.close = _open_up, _close_up, _open_dbl, _close_dbl
    return [("up", _up), ("dbl", _dbl)]


def test_pipeline_order_drop_lifecycle():
    events: list = []
    pipe = pipeline_svc.ItemPipeline(_stage_funcs(events), max_concurrency=2)
    kept, dropped, stats = run(pipe.run([{"v": 1}, {"v": 2}, {"v": 3}]))
    assert [k["v"] for k in kept] == [4, 8]  # order preserved, (v+1)*2
    assert dropped == [{"item": {"v": 2}, "reason": "bad value", "stage": "up"}]
    assert stats == {"received": 3, "kept": 2, "dropped": 1}
    assert events == ["open:up", "open:dbl", "close:dbl", "close:up"]


def test_pipeline_unexpected_error_propagates():
    async def _buggy(item):
        raise RuntimeError("stage bug")

    pipe = pipeline_svc.ItemPipeline([("buggy", _buggy)])
    with pytest.raises(RuntimeError):
        run(pipe.run([{"v": 1}]))


def test_pipeline_bounds_concurrency():
    in_flight = {"cur": 0, "max": 0}

    async def _slow(item):
        in_flight["cur"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["cur"])
        await asyncio.sleep(0.01)
        in_flight["cur"] -= 1
        return item

    pipe = pipeline_svc.ItemPipeline([("slow", _slow)], max_concurrency=3)
    kept, _, _ = run(pipe.run([{"v": i} for i in range(12)]))
    assert len(kept) == 12 and in_flight["max"] <= 3


def test_pipeline_cancel_between_batches():
    async def _ok(item):
        return item

    pipe = pipeline_svc.ItemPipeline([("ok", _ok)], max_concurrency=1)
    state = {"calls": 0}

    def _cancelled():
        state["calls"] += 1
        return state["calls"] > 1  # False for batch 1, True after

    kept, _, stats = run(pipe.run([{"v": i} for i in range(5)], cancelled=_cancelled))
    assert stats["kept"] == 2 and len(kept) == 2  # first batch of 2x1 drained


# -- runner pipeline wiring ----------------------------------------------------------------------
def test_runner_pipeline_validates_records():
    from app.services import runner as runner_svc

    plan = {"goal": "g", "entity": "Acme", "requested_count": 1, "max_results": 1,
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "search_queries": ["Acme"], "seed_domains": [],
            "seed_urls": ["https://x.example/a"], "source_types": [],
            "traversal": {"max_pages_per_domain": 1}, "validation_rules": [],
            "dedupe_keys": ["company_name"], "allowed_sources": [], "max_pages": 2}

    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        return {"url": url, "title": "Acme page",
                "html": "<html><body><p>Acme AI raises seed.</p></body></html>",
                "method": "http"}

    async def _llm(prompt, schema):
        return {"data": {"records": [
            {"fields": {"company_name": "Acme AI"},
             "evidence": [{"field": "company_name", "quote": "Acme AI",
                           "source_url": "https://x.example/a"}]}],
            "coverage": "full"}, "provider": "test"}

    stored: dict = {}

    async def _store(rid, pl, rows, counts):
        stored.update(rows=rows, counts=counts)
        return "d1"

    async def _persist(s):
        pass

    events: list = []

    async def _emit(ev):
        events.append(ev)

    ctx = {"search": _search, "fetch": _fetch, "llm": _llm, "store": _store,
           "persist_source": _persist, "cancelled": lambda: False}
    result = run(runner_svc.execute_run("r1", plan, ctx, _emit))
    assert result["status"] == "COMPLETED" and result["records"] == 1
    types = [e["type"] for e in events]
    assert "record.verified" in types and "run.completed" in types
    cell = stored["rows"][0]["fields"]["company_name"]
    assert cell["verification_status"] == "verified"
    assert cell["source"]["start"] is not None  # Phase-1 offsets survive the pipeline
    assert cell.get("normalized", {}).get("normalized") == "acme ai"


def test_run_completed_event_carries_record_counts():
    """run.completed data must carry records/verified/needs_review — the run
    view reads them from this event (a run showing records=0 with a full
    dataset was the stale-counter bug)."""
    from app.services import runner as runner_svc

    plan = {"goal": "g", "entity": "Acme", "requested_count": 1, "max_results": 1,
            "fields": [{"name": "company_name", "type": "string",
                        "description": "Name", "required": True}],
            "search_queries": ["Acme"], "seed_domains": [],
            "seed_urls": ["https://x.example/a"], "source_types": [],
            "traversal": {"max_pages_per_domain": 1}, "validation_rules": [],
            "dedupe_keys": ["company_name"], "allowed_sources": [], "max_pages": 2}

    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        return {"url": url, "title": "Acme page",
                "html": "<html><body><p>Acme AI raises seed.</p></body></html>",
                "method": "http"}

    async def _llm(prompt, schema):
        return {"data": {"records": [
            {"fields": {"company_name": "Acme AI"},
             "evidence": [{"field": "company_name", "quote": "Acme AI",
                           "source_url": "https://x.example/a"}]}],
            "coverage": "full"}, "provider": "test"}

    async def _store(rid, pl, rows, counts):
        return "d1"

    async def _persist(s):
        pass

    events: list = []

    async def _emit(ev):
        events.append(ev)

    ctx = {"search": _search, "fetch": _fetch, "llm": _llm, "store": _store,
           "persist_source": _persist, "cancelled": lambda: False}
    result = run(runner_svc.execute_run("r1", plan, ctx, _emit))
    done = next(e for e in events if e["type"] == "run.completed")
    assert done["data"]["records"] == result["records"] == 1
    assert done["data"]["verified"] >= 1
    assert "needs_review" in done["data"]

