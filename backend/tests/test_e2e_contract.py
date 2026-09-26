"""E2E contract tests: inputs/ -> pipeline -> outputs/ honesty guarantees.

No network, no keys. Proves: rule planner infers genuinely, seeds drive discovery
without Tavily, extraction without LLM yields ZERO records (anti-fabrication),
file fetcher serves real bytes, replay corpus quotes are genuine substrings.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.errors import AppError  # noqa: E402
from app.providers.crawl import file_fetch as file_fetch_mod  # noqa: E402
from app.services import discovery as discovery_svc  # noqa: E402
from app.services import extractor as extractor_svc  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_rule_planner_infers_genuinely():
    plan, provider = run(planner_svc.compile_plan(
        "Find 20 frontend engineering jobs in India with company, title, salary and source",
        llm=None))
    assert provider == "rule-based"
    assert plan["entity"] == "job" and plan["requested_count"] == 20
    names = {f["name"] for f in plan["fields"]}
    assert {"job_name", "salary", "location"} <= names
    planner_svc.coerce_plan(plan)


def test_rule_planner_count_bounds():
    plan, _ = run(planner_svc.compile_plan("Find 500 companies", llm=None))
    assert plan["requested_count"] == 50
    plan, _ = run(planner_svc.compile_plan("Find companies", llm=None))
    assert plan["requested_count"] == 15


def test_seeds_drive_discovery_without_tavily():
    async def _raising_search(q, limit, include=None, exclude=None):
        raise AppError("E_PROVIDER_FATAL", "no key", 424)

    async def _emit(ev):
        pass

    plan = {"goal": "g", "entity": "company", "requested_count": 5,
            "fields": [{"name": "company_name", "type": "string", "description": "",
                        "required": True}],
            "search_queries": [], "seed_domains": [], "seed_urls": ["https://s.example/a"],
            "source_types": [], "traversal": {"max_pages_per_domain": 3},
            "validation_rules": [], "dedupe_keys": ["company_name"],
            "allowed_sources": [], "max_pages": 5}
    out = run(discovery_svc.discover("run-9", plan, _raising_search, None, _emit))
    assert len(out) == 1 and out[0]["url"] == "https://s.example/a"
    assert out[0]["seeded"] is True and out[0]["route"] == "web:http"


def test_transient_search_retried_fatal_not():
    from app.core.errors import provider_transient
    calls = {"n": 0}

    async def _flaky(q, limit, include=None, exclude=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise provider_transient("blip")
        return [{"url": "https://s.example/a", "title": "Acme AI x",
                 "snippet": "Acme AI news"}]

    async def _emit(ev):
        pass

    plan = {"goal": "g", "entity": "Acme AI", "requested_count": 5,
            "fields": [{"name": "company_name", "type": "string", "description": "",
                        "required": True}],
            "search_queries": ["Acme AI"], "seed_domains": [], "seed_urls": [],
            "source_types": [], "traversal": {"max_pages_per_domain": 3},
            "validation_rules": [], "dedupe_keys": ["company_name"],
            "allowed_sources": [], "max_pages": 5}
    out = run(discovery_svc.discover("run-10", plan, _flaky, None, _emit))
    assert [a["url"] for a in out] == ["https://s.example/a"] and calls["n"] == 2


def test_no_fabrication_without_llm():
    html = "<html><body><h1>Acme AI raises $12.5M</h1><p>Founder Jane Doe.</p></body></html>"
    plan = {"fields": [{"name": "company_name", "type": "string", "description": "",
                        "required": True}]}
    recs, provider = run(extractor_svc.extract_page(
        plan, {"url": "https://s.example/a", "title": "t", "html": html}, None))
    assert recs == [] and provider == "none"


def test_file_fetcher_serves_real_bytes_and_refuses():
    fetch = file_fetch_mod.build(str(ROOT / "inputs" / "pages"),
                                 {"https://technews.example.com/acme-series-a": "acme.html"})
    page = run(fetch("https://technews.example.com/acme-series-a", "http"))
    assert "Acme AI raises $12.5M Series A" in page["html"] and page["method"] == "file"
    with pytest.raises(RuntimeError):
        run(fetch("https://unknown.example/x", "http"))
    evil = file_fetch_mod.build(str(ROOT / "inputs" / "pages"),
                                {"https://s.example/a": "../run.json"})
    with pytest.raises(RuntimeError):
        run(evil("https://s.example/a", "http"))


def test_replay_corpus_quotes_are_genuine():
    corpus = json.loads((ROOT / "inputs" / "replay_records.json").read_text(encoding="utf-8"))
    assert len(corpus["records"]) == 2
    for r in corpus["records"]:
        text = r["source_text"].lower()
        assert r["fields"] and r["evidence"]
        for e in r["evidence"]:
            norm = " ".join(e["quote"].split()).lower()
            assert norm in text, f"fabricated quote: {e['quote'][:60]}"
        assert r["source_url"] == r["evidence"][0]["source_url"]
