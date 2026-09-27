"""A plan must not be unpassable by construction.

Measured on a live run: the LLM planner returned 6 fields, all marked
`required: true`. Validation drops any record missing a required field, so the
gate rejected all 24 successfully extracted records - almost every one of them
solely because the page had no `website`. The run reported "COMPLETED, 0
records" and stored 943KB of perfectly good evidence that nothing could use.

The gate is behaving correctly here. The plan was the defect.
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


def _llm_returning(fields):
    prompts: list = []

    async def _llm(prompt, schema):
        prompts.append(prompt)
        return {"data": {"goal": "g", "entity": "company", "requested_count": 3,
                         "fields": fields, "search_queries": ["q"],
                         "dedupe_keys": ["company_name"], "max_pages": 6,
                         "allowed_sources": []},
                "provider": "test"}
    _llm.prompts = prompts
    return _llm


def test_blanket_required_is_capped(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    fields = [{"name": n, "type": "string", "description": n, "required": True}
              for n in ("company_name", "website", "annual_revenue",
                        "revenue_fiscal_year", "hq", "ceo")]
    plan, _ = run(planner_svc.compile_plan("ai companies", _llm_returning(fields)))
    required = [f["name"] for f in plan["fields"] if f["required"]]
    assert len(required) <= planner_svc.MAX_REQUIRED_FIELDS
    assert "company_name" in required, "the identifying field must stay required"


def test_the_identifying_field_is_never_demoted(monkeypatch):
    """The cap keeps the first required fields in declaration order, so the
    identifying attribute a planner lists first always survives."""
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    fields = [{"name": "company_name", "type": "string", "description": "",
               "required": True},
              {"name": "website", "type": "url", "description": "", "required": True},
              {"name": "hq", "type": "string", "description": "", "required": True},
              {"name": "revenue", "type": "number", "description": "", "required": True}]
    plan, _ = run(planner_svc.compile_plan("ai companies", _llm_returning(fields)))
    by_name = {f["name"]: f for f in plan["fields"]}
    assert by_name["company_name"]["required"] is True
    assert sum(1 for f in plan["fields"] if f["required"]) == \
        planner_svc.MAX_REQUIRED_FIELDS
    # The surplus at the tail is what gets demoted.
    assert by_name["revenue"]["required"] is False
    assert by_name["hq"]["required"] is False


def test_a_sensible_plan_is_left_alone(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    fields = [{"name": "company_name", "type": "string", "description": "",
               "required": True},
              {"name": "website", "type": "url", "description": "", "required": False}]
    plan, _ = run(planner_svc.compile_plan("ai companies", _llm_returning(fields)))
    by_name = {f["name"]: f for f in plan["fields"]}
    assert by_name["company_name"]["required"] is True
    assert by_name["website"]["required"] is False


def test_the_prompt_tells_the_model_not_to_require_everything(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)
    fields = [{"name": "company_name", "type": "string", "description": "",
               "required": True}]
    llm = _llm_returning(fields)
    run(planner_svc.compile_plan("ai companies", llm))
    assert llm.prompts, "no prompt captured"
    p = llm.prompts[0]
    assert "required=false" in p
    assert "Never mark every field required" in p


def test_cap_applies_to_the_rule_based_rung(monkeypatch):
    """Every rung, so the invariant does not depend on which model answered."""
    plan = {"fields": [{"name": f"f{i}", "type": "string", "description": "",
                        "required": True} for i in range(7)]}
    capped = planner_svc._cap_required(plan)
    assert sum(1 for f in capped["fields"] if f["required"]) == \
        planner_svc.MAX_REQUIRED_FIELDS


def test_cap_leaves_a_small_required_set_intact():
    plan = {"fields": [{"name": "a", "required": True}, {"name": "b", "required": False}]}
    assert planner_svc._cap_required(plan) == plan


def test_a_capped_plan_can_actually_pass_the_gate():
    """End to end through the real gate: a record missing only optional fields
    survives, which is the whole point of capping."""
    from app.services import validator as validator_svc

    fields = [{"name": "company_name", "type": "string", "required": True},
              {"name": "website", "type": "url", "required": False}]
    page = "Acme Corp is a European AI company."
    raw = {"fields": {"company_name": "Acme Corp"},
           "evidence": [{"field": "company_name", "value": "Acme Corp",
                         "quote": "Acme Corp is a European AI company"}]}
    wrapped = run(validator_svc.wrap_record(fields, raw, page, "u", "t"))
    required = {f["name"] for f in fields if f["required"]}
    missing = [n for n in required if wrapped.get(n, {}).get("value") in (None, "")]
    assert not missing, "a record with its required field evidenced must survive"
