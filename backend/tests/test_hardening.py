"""Hardening tests: robots/throttle/traversal/domains/supervisor — no network, no keys.

Robots parser is canned via monkeypatch (no HTTP). Supervisor runs the compiled
LangGraph when installed, else the identical manual path — both asserted through
run_supervisor's public contract.
"""
import asyncio
import sys
import time
import urllib.robotparser
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.graph import run_supervisor  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402
from app.providers.llm import langchain_client as lc  # noqa: E402
from app.providers.search import tavily  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# -- robots (canned fetch, no network) --------------------------------------------
def _canned(monkeypatch, host, text):
    async def _text(h, s):
        return text
    monkeypatch.setattr(fetcher, "_fetch_robots_text", _text)
    fetcher._robots.pop(host, None)


def test_robots_disallow_and_delay(monkeypatch):
    _canned(monkeypatch, "x.example",
            "User-agent: *\nDisallow: /private\nCrawl-delay: 2\n")
    allowed, delay = run(fetcher.robots_allowed("https://x.example/public"))
    assert allowed is True and delay == 2.0
    allowed, _ = run(fetcher.robots_allowed("https://x.example/private/a"))
    assert allowed is False


def test_robots_unreadable_and_401(monkeypatch):
    _canned(monkeypatch, "down.example", None)  # unreadable -> allow
    fetcher._robots.pop("down.example", None)
    assert run(fetcher.robots_allowed("https://down.example/a")) == (True, 0.0)
    _canned(monkeypatch, "walled.example", "DISALLOW-ALL")  # 401/403 -> deny
    fetcher._robots.pop("walled.example", None)
    assert run(fetcher.robots_allowed("https://walled.example/a")) == (False, 0.0)


def test_robots_never_raises(monkeypatch):
    async def _boom(host, scheme):
        raise OSError("net")
    monkeypatch.setattr(fetcher, "_fetch_robots_text", _boom)
    fetcher._robots.pop("down.example", None)
    assert run(fetcher.robots_allowed("https://down.example/a")) == (True, 0.0)


# -- throttle / backoff ------------------------------------------------------------
def test_throttle_enforces_interval():
    host = "throttle-test.example"
    fetcher._last_hit.pop(host, None)
    run(fetcher.throttle(host))
    t0 = time.monotonic()
    run(fetcher.throttle(host))
    assert time.monotonic() - t0 >= 0.9


def test_backoff_bounded():
    t0 = time.monotonic()
    run(fetcher.backoff(0))
    assert time.monotonic() - t0 < 3.0


# -- links ---------------------------------------------------------------------------
def test_extract_links_same_host_only():
    html = ('<a href="/b">b</a><a href="https://x.example/c">c</a>'
            '<a href="https://evil.example/d">d</a><a href="ftp://x.example/e">e</a>'
            '<a href="mailto:a@x.example">m</a>')
    links = fetcher.extract_links(html, "https://x.example/a")
    assert links == ["https://x.example/b", "https://x.example/c"]


def test_extract_links_cap_and_bad_base():
    html = "".join(f'<a href="/p{i}">x</a>' for i in range(100))
    assert len(fetcher.extract_links(html, "https://x.example/a", limit=5)) == 5
    assert fetcher.extract_links(html, "::::") == []


# -- media inventory (pure, no network) --------------------------------------------------
def test_extract_media_images_videos():
    html = ('<img src="/i/a.png" alt="Diagram A">'
            '<img data-src="https://cdn.example/b.jpg">'
            '<img src="data:image/png;base64,xx" alt="inline">'
            '<video src="/v/c.mp4"></video>'
            '<iframe src="https://www.youtube.com/embed/xyz"></iframe>'
            '<a href="/files/d.mov">download</a>'
            '<a href="/page">normal</a>')
    media = fetcher.extract_media(html, "https://x.example/p")
    assert {"src": "https://x.example/i/a.png", "alt": "Diagram A"} in media["images"]
    assert {"src": "https://cdn.example/b.jpg", "alt": ""} in media["images"]
    assert all(not i["src"].startswith("data:") for i in media["images"])
    assert "https://x.example/v/c.mp4" in media["videos"]
    assert "https://www.youtube.com/embed/xyz" in media["videos"]
    assert "https://x.example/files/d.mov" in media["videos"]
    assert len(media["videos"]) == 3
    assert fetcher.extract_media("", "https://x.example/") == {"images": [], "videos": []}


def test_crawl_result_media_mapping():
    res = SimpleNamespace(
        media={"images": [{"src": "/i/a.png", "alt": "A"},
                          {"src": "", "alt": "empty"},
                          "junk",
                          {"src": "ftp://x/y.png", "alt": "bad-scheme"}],
               "videos": [{"src": "/v/c.mp4"}, {"src": ""}]},
        links={"internal": [{"href": "/a", "text": "A"}],
               "external": [{"href": "https://other.example/b", "text": "B"},
                            {"href": "mailto:x@y", "text": "mail"}]})
    media = fetcher._crawl_result_media(res, "https://x.example/p")
    assert media["images"] == [{"src": "https://x.example/i/a.png", "alt": "A"}]
    assert media["videos"] == ["https://x.example/v/c.mp4"]
    links = fetcher._crawl_result_links(res, "https://x.example/p")
    assert {"url": "https://x.example/a", "text": "A", "internal": True} in links
    assert {"url": "https://other.example/b", "text": "B", "internal": False} in links
    assert all("mailto" not in l["url"] for l in links)
    assert fetcher._crawl_result_media(object(), "u") == {"images": [], "videos": []}
    assert fetcher._crawl_result_links(object(), "u") == []


def test_crawl_result_page_snapshot():
    md = SimpleNamespace(fit_markdown="fit text", raw_markdown="raw text",
                         references_markdown="## References")
    res = SimpleNamespace(success=True, title="T",
                          markdown=md, html="<html><body>full dom</body></html>",
                          media={"images": [], "videos": []}, links={})
    page = fetcher._crawl_result_page(res, "https://x.example/a")
    assert page["markdown"] == "fit text" and page["markdown_source"] == "fit"
    assert page["rendered_html"] == "<html><body>full dom</body></html>"
    assert page["snapshot_chars"] == len("<html><body>full dom</body></html>")
    assert page["method"] == "crawl4ai" and page["references"] == "## References"
    empty = fetcher._crawl_result_page(SimpleNamespace(), "u")
    assert empty["markdown"] == "" and empty["rendered_html"] == ""
    assert empty["markdown_source"] == "none" and empty["snapshot_chars"] == 0


def test_reducer_keeps_tables():
    html = ("<html><body><p>Intro</p><table><tr><th>Name</th><th>Price</th></tr>"
            "<tr><td>Acme</td><td>$12</td></tr></table></body></html>")
    text, rung = reducer_svc.reduce_html_with_rung(html)
    assert "Acme" in text and "$12" in text and rung == "trafilatura"


def test_extract_structured_json_ld_and_meta():
    html = ("""<html><head>
<script type="application/ld+json">{"@context": "https://schema.org",
"@type": "NewsArticle", "headline": "Big Launch",
"author": {"@type": "Person", "name": "Ada Lovelace"},
"datePublished": "2024-05-01", "articleBody": "Full body here."}</script>
<meta name="description" content="Short desc">
<meta name="keywords" content="ai, startups">
</head><body><p>Hi</p></body></html>""")
    out = reducer_svc.extract_structured(html)
    assert out["author"] == "Ada Lovelace"
    assert out["published"] == "2024-05-01"
    assert out["description"] == "Short desc"
    assert out["tags"] == ["ai", "startups"]
    assert "Full body here." in out["body"]
    assert "Ada Lovelace" in out["text"] and "2024-05-01" in out["text"]


def test_extract_structured_meta_fallback_and_empty():
    html = ('<html><head><meta name="author" content="Grace Hopper">'
            '<meta property="article:published_time" content="2023-01-02">'
            "</head><body><p>Hi</p></body></html>")
    out = reducer_svc.extract_structured(html)
    assert out["author"] == "Grace Hopper" and out["published"] == "2023-01-02"
    assert reducer_svc.extract_structured("")["text"] == ""
    assert reducer_svc.extract_structured("<html><body><p>Hi</p></body></html>")["text"] == ""


def test_traversal_domain_cap_respects_settings(monkeypatch):
    from app.core import config as config_mod
    assert config_mod.settings.RUN_MAX_DOMAIN_PAGES >= 10
    monkeypatch.setattr(config_mod.settings, "RUN_MAX_PAGES", 40)

    async def _fetch(url, method):
        if url == "https://x.example/a":
            links = "".join(f'<a href="/p{i}">x</a>' for i in range(30))
            return _page(url, links)
        return _page(url)

    async def _emit(ev):
        pass

    async def _persist(s):
        pass

    plan = {"max_pages": 40, "traversal": {"max_pages_per_domain": 30}}
    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist, plan))
    cap = config_mod.settings.RUN_MAX_DOMAIN_PAGES
    assert counts["successful"] == cap  # cap counts the seed's domain too


def test_media_counts_persisted():
    persisted = []

    async def _fetch(url, method):
        return {"url": url, "title": "t",
                "html": '<img src="/i.png" alt="I"><a href="/b">b</a>',
                "images": [{"src": "https://x.example/i.png", "alt": "I"}],
                "videos": [], "method": "http"}

    async def _emit(ev):
        pass

    async def _persist(s):
        persisted.append(s)

    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist,
                                              {"max_pages": 2,
                                               "traversal": {"max_pages_per_domain": 1}}))
    ok_sources = [s for s in persisted if s["status"] == "ok"]
    assert ok_sources and ok_sources[0]["media"] == {"images": 1, "videos": 0, "links": 1}
    assert ok_sources[0]["snapshot_chars"] > 0  # snapshot size persisted


# -- tavily domains ---------------------------------------------------------------------
def test_tavily_missing_key_fails_named():
    with pytest.raises(AppError) as ei:
        run(tavily.search("q", 1))
    assert ei.value.code == "E_PROVIDER_FATAL"


# -- langchain seam -----------------------------------------------------------------------
def test_langchain_decide_no_keys_raises():
    with pytest.raises(RuntimeError):
        run(lc.decide("prompt", 8, []))


# -- supervisor end-to-end (stub deps, no network/keys) --------------------------------------
def _deps(results):
    async def _search(q, limit, include=None, exclude=None):
        assert include == [] and exclude == []
        return results[:limit]
    from app.providers.decision import jev
    from app.services.source_router import triage_source
    return SimpleNamespace(search_fn=_search, llm_strategy=None,
                           jev_screen=jev.source_screening,
                           jev_continuation=jev.research_continuation,
                           triage_fn=triage_source, include_domains=[], exclude_domains=[],
                           max_pages=5, max_queries=8, results_per_query=5)


def _initial():
    return {"run_id": "r1", "goal": "Find Acme", "entity": "Acme AI", "fields": [],
            "queries": ["Acme AI funding"], "searched_queries": [], "candidate_urls": [],
            "screened_urls": [], "accepted_sources": [], "extracted_records": [],
            "rejected_records": [],             "requested_count": 5, "valid_count": 0,
            "missing_fields": [], "iteration": 0, "max_iterations": 3,
            "last_searched": 1, "decision": "", "decision_reason": ""}


def test_supervisor_accepts_and_budgets():
    results = [{"url": f"https://x.example/{i}", "title": f"Acme AI news {i}",
                "snippet": "Acme AI raises funding"} for i in range(10)]
    final = run(run_supervisor(_initial(), _deps(results), "r1"))
    assert len(final["accepted_sources"]) == 5  # max_pages cap
    assert final["searched_queries"] == ["Acme AI funding"]  # no LLM: single round
    assert final["iteration"] == 1


def test_supervisor_screens_irrelevant():
    results = [{"url": "https://x.example/a", "title": "Acme AI raises",
                "snippet": "Acme AI funding"},
               {"url": "https://y.example/b", "title": "Baking recipes",
                "snippet": "sourdough bread"}]
    final = run(run_supervisor(_initial(), _deps(results), "r1"))
    assert [a["url"] for a in final["accepted_sources"]] == ["https://x.example/a"]


def test_strict_supervisor_skips_langchain_cloud(monkeypatch):
    """OPENCODE_STRICT: supervisor iterations use llm_strategy (opencode)
    directly — the LangChain Groq/Gemini rung never fires."""
    from app.core import config as config_mod
    from app.providers.llm import langchain_client as lc

    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)

    def _boom(*a, **k):
        raise AssertionError("strict mode must not call langchain cloud")

    monkeypatch.setattr(lc, "decide", _boom)

    async def _strategy(prompt, schema):
        return {"decision": "FETCH", "reason": "strict",
                "missing_coverage": [], "next_queries": [], "confidence": 0.9}

    from app.providers.decision import jev
    from app.services.source_router import triage_source
    deps = SimpleNamespace(search_fn=_deps([]).search_fn, llm_strategy=_strategy,
                           jev_screen=jev.source_screening,
                           jev_continuation=jev.research_continuation,
                           triage_fn=triage_source, include_domains=[],
                           exclude_domains=[], max_pages=5, max_queries=8,
                           results_per_query=5)
    st = _initial()
    st["accepted_sources"] = [{"url": "https://x.example/a", "title": "t"}]
    final = run(run_supervisor(st, deps, "r1"))
    assert "https://x.example/a" in [a["url"] for a in final["accepted_sources"]]


def test_screen_node_batches_concurrent_with_cap():
    """Screens run concurrently (batches of 5), decisions apply in order,
    the max_pages cap holds, and every fresh URL is marked screened."""
    from app.agents.graph import screen_node

    state_calls = {"cur": 0, "max": 0}

    async def _screen(url, title, snippet, entity):
        state_calls["cur"] += 1
        state_calls["max"] = max(state_calls["max"], state_calls["cur"])
        try:
            await asyncio.sleep(0.1)
            if "nope" in url:
                return {"judgment": "NO", "confidence": 0.9}
            return {"judgment": "YES", "confidence": 0.8}
        finally:
            state_calls["cur"] -= 1

    fresh = [{"url": f"https://x.example/p{i}", "title": "t", "snippet": "s"}
             for i in range(12)]
    fresh[3] = {"url": "https://x.example/nope", "title": "t", "snippet": "s"}
    deps = SimpleNamespace(jev_screen=_screen, triage_fn=lambda u: "web:http",
                           max_pages=5)
    t0 = time.perf_counter()
    out = run(screen_node({"candidate_urls": fresh, "screened_urls": [],
                           "accepted_sources": [], "entity": "startup"},
                          deps))
    elapsed = time.perf_counter() - t0
    assert state_calls["max"] >= 2  # genuinely concurrent
    assert elapsed < 0.9  # sequential would take 12 x 0.1 = 1.2s
    assert len(out["accepted_sources"]) == 5  # cap holds
    assert [a["url"] for a in out["accepted_sources"]] == [
        f"https://x.example/p{i}" for i in (0, 1, 2, 4, 5)]  # order kept, NO skipped
    assert sorted(out["screened_urls"]) == sorted(r["url"] for r in fresh)


def test_crawl_runs_on_dedicated_executor(monkeypatch):
    """Crawl4AI blocking work must run on _CRAWL_EXECUTOR threads, never the
    default pool — orphaned browser threads starved the shared pool into a
    total idle-loop deadlock (regression)."""
    import threading

    seen_threads: list = []

    def _fake_crawl(url, timeout_s):
        seen_threads.append(threading.current_thread().name)
        return {"url": url, "title": "t", "markdown": "hi", "method": "crawl4ai"}

    async def _guard(url):
        return url

    async def _robots(url):
        return True, 0.0

    async def _throttle(host, floor=0.0):
        return None

    monkeypatch.setattr(fetcher, "_sync_crawl", _fake_crawl)
    monkeypatch.setattr(fetcher, "aguard_url", _guard)
    monkeypatch.setattr(fetcher, "robots_allowed", _robots)
    monkeypatch.setattr(fetcher, "throttle", _throttle)
    page = run(fetcher.crawl4ai_fetch("https://x.example/a"))
    assert page["method"] == "crawl4ai"
    assert seen_threads and all(t.startswith("dg-crawl") for t in seen_threads)


# -- crawler traversal + skipped ---------------------------------------------------------------
def _page(url, html=""):
    return {"url": url, "final_url": url, "title": "t", "html": html or f"<p>{url}</p>",
            "method": "http"}


def test_fetch_traversal_depth_and_caps():
    seen_calls = []

    async def _fetch(url, method):
        seen_calls.append(url)
        if url == "https://x.example/a":
            return _page(url, '<a href="/b">b</a><a href="/c">c</a>'
                              '<a href="https://other.example/z">z</a>')
        return _page(url)

    async def _emit(ev):
        pass

    persisted = []

    async def _persist(s):
        persisted.append(s)

    plan = {"max_pages": 5, "traversal": {"max_pages_per_domain": 3}}
    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist, plan))
    urls = sorted(p["url"] for p in pages)
    assert urls == ["https://x.example/a", "https://x.example/b", "https://x.example/c"]
    assert {p.get("depth", -1) for p in pages} == {0, 1}
    assert counts == {"attempted": 3, "successful": 3, "failed": 0, "skipped": 0}
    assert all(s["status"] == "ok" for s in persisted)
    assert "https://other.example/z" not in seen_calls  # cross-host never traversed


def test_fetch_skipped_counted_not_failed():
    async def _fetch(url, method):
        return {"url": url, "skipped": "robots-disallowed", "method": "http"}

    async def _emit(ev):
        pass

    persisted = []

    async def _persist(s):
        persisted.append(s)

    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist, {}))
    assert pages == [] and counts["skipped"] == 1 and counts["failed"] == 0
    assert persisted[0]["status"] == "skipped"


def test_fetch_per_domain_cap():
    async def _fetch(url, method):
        if url == "https://x.example/a":
            links = "".join(f'<a href="/p{i}">x</a>' for i in range(10))
            return _page(url, links)
        return _page(url)

    async def _emit(ev):
        pass

    async def _persist(s):
        pass

    plan = {"max_pages": 20, "traversal": {"max_pages_per_domain": 2}}
    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist, plan))
    assert counts["successful"] == 2  # seed + 1 (cap counts the seed's domain too)
    assert all(len(p.get("content_hash", "")) == 64 for p in pages)  # sha256 wired


def test_fetch_all_worker_pool_exact_under_overlap():
    """4 workers overlap I/O but counts stay exact: budget never overshoots,
    every settled URL is fetched and persisted exactly once."""
    state = {"cur": 0, "max": 0}
    seen_calls: list[str] = []

    async def _fetch(url, method):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        try:
            await asyncio.sleep(0.02)  # force worker overlap
            seen_calls.append(url)
            if url == "https://x.example/a":
                links = "".join(f'<a href="/p{i}">link number {i} here</a>'
                                for i in range(6))
                return _page(url, f"<p>{'body text ' * 40}</p>" + links)
            return _page(url, f"<p>{'body text ' * 40}</p>")
        finally:
            state["cur"] -= 1

    async def _emit(ev):
        pass

    persisted = []

    async def _persist(s):
        persisted.append(s)

    plan = {"max_pages": 5, "traversal": {"max_pages_per_domain": 10}}
    pages, counts = run(crawler_svc.fetch_all([{"url": "https://x.example/a", "title": ""}],
                                              _fetch, _emit, _persist, plan))
    assert state["max"] >= 2  # workers genuinely overlapped
    assert counts == {"attempted": 5, "successful": 5, "failed": 0, "skipped": 0}
    assert sorted({p["url"] for p in pages}) == sorted(set(seen_calls))
    assert len([s for s in persisted if s["status"] == "ok"]) == 5


def test_judge_confirms_and_adopts(monkeypatch):
    from app.providers.decision import jev
    from app.services import deduper as dd

    def _row2(val, rival):
        return {"fields": {"founded": {"value": val, "verification_status": "conflicting",
                                       "source": {"url": "a", "title": "t", "quote": val,
                                                  "retrieved_at": "now"},
                                       "rivals": [{"value": rival,
                                                   "source": {"url": "b", "title": "t",
                                                              "quote": rival,
                                                              "retrieved_at": "now"}}]}}}

    stats = run(dd.adjudicate_conflicts([_row2("2023", "2024")]))
    assert stats == {"judged": 1, "confirmed": 1, "adopted": 0, "downgraded": 0}  # stub: CONFLICT

    async def _b(field, va, vb, qa="", qb=""):
        return {"decision": "B", "provider": "test"}
    monkeypatch.setattr(jev, "conflict_triage", _b)
    rows = [_row2("2023", "2024")]
    stats = run(dd.adjudicate_conflicts(rows))
    assert stats["adopted"] == 1
    assert rows[0]["fields"]["founded"]["value"] == "2024"
    assert rows[0]["fields"]["founded"]["verification_status"] == "verified"

    async def _ins(field, va, vb, qa="", qb=""):
        return {"decision": "INSUFFICIENT", "provider": "test"}
    monkeypatch.setattr(jev, "conflict_triage", _ins)
    rows = [_row2("2023", "2024")]
    stats = run(dd.adjudicate_conflicts(rows))
    assert stats["downgraded"] == 1
    assert rows[0]["fields"]["founded"] == {"value": None, "verification_status": "unverified",
                                            "source": {"url": "a", "title": "t", "quote": "2023",
                                                       "retrieved_at": "now"},
                                            "rivals": rows[0]["fields"]["founded"]["rivals"]}


def test_local_repo_saves_exports(tmp_path):
    from app.repositories.local_repo import LocalRepo
    repo = LocalRepo(root=str(tmp_path / "ldb"))
    eid = repo.save_export("d1", "csv", 128)
    assert eid and repo._scan("exports")[0]["byte_size"] == 128
