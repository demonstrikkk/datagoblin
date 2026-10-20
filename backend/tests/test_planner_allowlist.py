"""An allowlist the planner invented must not become a filter the run obeys.

`allowed_sources` is a *hard* filter in two places — Tavily's `include_domains`
and the traversal allowlist. So a domain the planner made up is a domain the run
can never leave.

Measured: the prompt "Find European logistics companies with their chief
executive and headquarters city" produced the allowlist
`["wikipedia.org", "official company websites", "dnb.com", "statista.com"]`. The
primary search was then pinned to those domains, returned nothing crawlable, and
every source the run actually used came from an unrestricted fallback path. One
entry (`"official company websites"`) is not even a domain. The planner was
answering a question nobody asked, which is the defect `_cap_total_fields`
already exists to stop for fields.

The rule: keep an allowlist entry only when the prompt itself named it.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.services import discovery as discovery_svc  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402

LIVE_PROMPT = ("Find European logistics companies with their chief executive "
               "and headquarters city")
LIVE_FABRICATED = ["wikipedia.org", "official company websites", "dnb.com",
                   "statista.com"]


def run(coro):
    return asyncio.run(coro)


def _llm_returning(sources):
    async def _llm(prompt, schema):
        return {"data": {"goal": "g", "entity": "company", "requested_count": 3,
                         "fields": [{"name": "company_name", "type": "string",
                                     "description": "", "required": True}],
                         "search_queries": ["q"], "dedupe_keys": ["company_name"],
                         "max_pages": 6, "allowed_sources": list(sources)},
                "provider": "test"}
    return _llm


def test_the_fabricated_allowlist_is_dropped(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    plan, _ = run(planner_svc.compile_plan(LIVE_PROMPT,
                                           _llm_returning(LIVE_FABRICATED)))
    assert plan["allowed_sources"] == []


def test_a_domain_the_prompt_named_is_kept(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    plan, _ = run(planner_svc.compile_plan(
        "List logistics firms, only from dnb.com", _llm_returning(LIVE_FABRICATED)))
    assert plan["allowed_sources"] == ["dnb.com"]


def test_the_dropped_allowlist_unblocks_the_search(monkeypatch):
    """The consequence, not just the field: `_domains` feeds Tavily's include."""
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    plan, _ = run(planner_svc.compile_plan(LIVE_PROMPT,
                                           _llm_returning(LIVE_FABRICATED)))
    include, _exclude = discovery_svc._domains(plan)
    assert include == [], "an invented allowlist must not pin the search"


def test_a_named_domain_still_constrains_the_search(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    plan, _ = run(planner_svc.compile_plan(
        "List logistics firms, only from dnb.com", _llm_returning(LIVE_FABRICATED)))
    include, _exclude = discovery_svc._domains(plan)
    assert include == ["dnb.com"]


def test_a_phrase_that_is_not_a_domain_is_dropped():
    kept = planner_svc._requested_allowed_sources(
        "", ["official company websites", "wikipedia.org"])
    assert kept == [], "a phrase is not a host and matches nothing"


def test_a_url_entry_is_matched_by_its_host():
    assert planner_svc._requested_allowed_sources(
        "data from y.example please", ["https://y.example/s"]) == ["https://y.example/s"]
    assert planner_svc._requested_allowed_sources(
        "no domains here", ["https://y.example/s"]) == []


def test_blank_entries_are_dropped():
    assert planner_svc._requested_allowed_sources("dnb.com", ["", "  ", "dnb.com"]) == ["dnb.com"]


def test_the_rule_based_rung_is_sanitised_too(monkeypatch):
    """Every rung shares `_finalize`, so the invariant cannot hold on one only."""
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    plan, provider = run(planner_svc.compile_plan("find 5 logistics companies"))
    assert provider == "rule-based"
    assert plan["allowed_sources"] == []
