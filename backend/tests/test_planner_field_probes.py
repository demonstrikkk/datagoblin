"""Discovery can only surface pages for what it searches for.

Measured on a live European-logistics run: the crawl's identity columns were
100% filled (`company_name` 17/17, `hq_city` 17/17) while the per-entity columns
were not (`chief_executive_name` 2/17, `website` 1/17). The scrape had queued 49
same-host children and fetched none of them because the page budget was spent on
the seeds -- so this is about both the *queries* (asking for the field) and the
*budget* (paying for the page that answers it).

The probe rule is deliberately conservative: a field that an existing query
already names gets no probe. The live run's queries already said "chief
executive" and "headquarters", so adding probes there would have been duplicate
spend for no new pages.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _llm(fields, queries, *, count=5, max_pages=6,
         dedupe=("company_name",)):
    async def _call(prompt, schema):
        return {"data": {"goal": "g", "entity": "company",
                         "requested_count": count,
                         "fields": list(fields),
                         "search_queries": list(queries),
                         "dedupe_keys": list(dedupe),
                         "max_pages": max_pages,
                         "allowed_sources": []},
                "provider": "test"}
    return _call


NAME = {"name": "company_name", "type": "string",
        "description": "", "required": True}
CEO = {"name": "chief_executive_name", "type": "string",
       "description": "", "required": False}
SITE = {"name": "website", "type": "url", "description": "", "required": False}
EMPLOYEES = {"name": "employee_count", "type": "number",
             "description": "", "required": False}


def _plan(fields, queries, **kw):
    plan, _ = run(planner_svc.compile_plan(
        "logistics companies", _llm(fields, queries, **kw)))
    return plan


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)


def test_a_query_is_added_for_a_field_no_query_names():
    plan = _plan([NAME, CEO, SITE], ["logistics companies europe"])
    joined = " | ".join(plan["search_queries"]).lower()
    assert "chief executive" in joined
    assert "official website" in joined


def test_no_probe_when_the_query_already_names_the_field():
    """The live logistics case: probing a field the query already names is spend
    that returns the same pages again."""
    queries = ["logistics companies europe chief executive official website"]
    plan = _plan([NAME, CEO, SITE], queries)
    assert plan["search_queries"] == queries


def test_an_acronym_and_a_synonym_count_as_naming_the_field():
    """The live plan said "CEOs" and "headquarters"; the field names say "chief"
    and "hq". Neither probe may fire -- otherwise both slots go to fields that
    are already asked for and the two empty columns never get one."""
    queries = ["european logistics companies and their CEOs",
               "3PL providers europe headquarters",
               "largest logistics companies list"]
    plan = _plan([NAME, CEO, SITE, EMPLOYEES,
                  {"name": "hq_city", "type": "string"}], queries)
    joined = " | ".join(plan["search_queries"]).lower()
    assert "chief executive" not in joined
    assert "headquarters city" not in joined
    assert "official website" in joined
    assert "number of employees" in joined


def test_the_most_specific_query_survives_making_room():
    queries = ["largest companies in europe",
               "logistics providers europe",
               "european logistics companies list",
               "european transport companies leadership",
               "DHL Kuehne Nagel DSV DB Schenker DPD"]
    plan = _plan([NAME, SITE, EMPLOYEES], queries)
    assert "DHL Kuehne Nagel DSV DB Schenker DPD" in plan["search_queries"]
    assert "largest companies in europe" not in plan["search_queries"]
    assert len(plan["search_queries"]) == 5


def test_identity_fields_are_never_probed():
    plan = _plan([NAME], ["q"])
    assert plan["search_queries"] == ["q"]


def test_the_five_query_limit_is_never_exceeded():
    fields = [NAME, CEO, SITE, EMPLOYEES,
              {"name": "industry_focus", "type": "string"},
              {"name": "hq_city", "type": "string"},
              {"name": "hq_country", "type": "string"}]
    plan = _plan(fields, ["a", "b", "c", "d", "e"])
    assert len(plan["search_queries"]) <= 5
    planner_svc.coerce_plan(plan)  # still satisfies the plan contract


def test_a_property_plan_reserves_a_detail_page_per_record(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "RUN_MAX_PAGES", 30)
    plan = _plan([NAME, CEO], ["q"], count=20, max_pages=6)
    assert plan["max_pages"] == 26
    assert plan["traversal"]["max_pages_per_domain"] >= 15
    assert plan["traversal"]["max_children_per_page"] >= 12


def test_the_page_budget_never_exceeds_the_configured_ceiling():
    plan = _plan([NAME, CEO], ["q"], count=200, max_pages=6)
    assert plan["max_pages"] <= config_mod.settings.RUN_MAX_PAGES


def test_an_identity_only_plan_keeps_its_budget():
    plan = _plan([NAME], ["q"], count=5, max_pages=6)
    assert plan["max_pages"] == 6
    assert "max_children_per_page" not in plan["traversal"]


def test_the_rule_based_rung_gets_the_same_treatment():
    """Every rung shares `_finalize`, so the behaviour cannot depend on which
    planner answered."""
    plan, provider = run(planner_svc.compile_plan(
        "Find 5 companies and their founders", llm=None))
    assert provider == "rule-based"
    joined = " | ".join(plan["search_queries"]).lower()
    assert "founder" in joined
    assert plan["traversal"]["max_children_per_page"] >= 12
    assert plan["traversal"]["max_pages_per_domain"] >= 15
