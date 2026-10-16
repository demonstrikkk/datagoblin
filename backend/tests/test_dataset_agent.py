"""The dataset agent answers with tools, never with model-authored queries.

The properties under test are all about what the model is NOT allowed to do,
so none of these need a model: the LLM is a test double returning canned JSON,
and the assertions pin the server-side enforcement.

  - a plan naming an unknown tool, an unknown field, or more calls than the
    cap is cut down to what is executable, never executed as written
  - a chart must be copied from a distribution a tool returned — same labels,
    same counts — or it is refused
  - a change request never reaches the model at all
  - the default plan (model failure) still answers from real measurements
"""
import asyncio

import pytest

from app.services import dataset_agent as agent


def _cell(value, status="verified"):
    return {"value": value, "verification_status": status,
            "source": {"url": "https://x.test/p", "title": "t", "quote": value}}


class _Store:
    def __init__(self, records):
        self._records = records

    def get_dataset_schema(self, did):
        return [{"name": "company"}, {"name": "role"}]

    def get_dataset(self, did):
        return {"id": did, "run_id": "r1", "name": "n",
                "schema": self.get_dataset_schema(did),
                "records": self._records}

    def get_run(self, rid):
        return {}

    def get_records(self, did, q="", limit=100, offset=0):
        return {"records": self._records, "total": len(self._records)}


def _records():
    return [
        {"record_id": "1", "fields": {"company": _cell("Acme"), "role": _cell("eng")}},
        {"record_id": "2", "fields": {"company": _cell("Acme"), "role": _cell("ml")}},
        {"record_id": "3", "fields": {"company": _cell("Beta"), "role": _cell("eng")}},
    ]


def _llm(planned, answer):
    async def fake(prompt, schema):
        if "picking read tools" in prompt:
            return {"data": planned, "provider": "test", "model": "test"}
        return {"data": answer, "provider": "test", "model": "test"}
    return fake


def _answer_payload(**over):
    base = {"answer": "Acme appears twice of three records.",
            "figures": [{"label": "Acme share", "value": "2 of 3"}],
            "chart": {"title": "company — by count",
                      "items": [{"label": "Acme", "n": 2},
                                {"label": "Beta", "n": 1}]}}
    base.update(over)
    return base


def test_an_unknown_tool_in_the_plan_is_dropped():
    plan = agent._validate_plan(
        {"tool_calls": [{"tool": "run_sql", "args": {"sql": "DROP TABLE"}},
                        {"tool": "value_counts",
                         "args": {"field": "company"}}]},
        ["company", "role"])
    assert [c["tool"] for c in plan] == ["dataset_facts", "value_counts"]


def test_an_unknown_field_in_the_plan_is_dropped():
    plan = agent._validate_plan(
        {"tool_calls": [{"tool": "value_counts",
                         "args": {"field": "salary"}}]},
        ["company", "role"])
    assert plan == []


def test_the_plan_is_capped():
    plan = agent._validate_plan(
        {"tool_calls": [{"tool": "coverage", "args": {}}] * 9},
        ["company"])
    assert len(plan) <= agent.MAX_TOOL_CALLS


def test_a_chart_copied_from_a_tool_result_is_kept():
    results = [{"tool": "value_counts", "args": {"field": "company"},
                "result": {"values": [{"value": "Acme", "n": 2},
                                      {"value": "Beta", "n": 1}]},
                "summary": ""}]
    chart = agent._validate_chart(
        {"title": "t", "items": [{"label": "Acme", "n": 2},
                                {"label": "Beta", "n": 1}]}, results)
    assert chart is not None
    assert chart["items"][0] == {"label": "Acme", "n": 2}


def test_a_chart_with_invented_numbers_is_refused():
    results = [{"tool": "value_counts", "args": {"field": "company"},
                "result": {"values": [{"value": "Acme", "n": 2},
                                      {"value": "Beta", "n": 1}]},
                "summary": ""}]
    assert agent._validate_chart(
        {"title": "t", "items": [{"label": "Acme", "n": 99},
                                {"label": "Beta", "n": 1}]}, results) is None


def test_a_single_bar_is_not_a_chart():
    assert agent._validate_chart(
        {"title": "t", "items": [{"label": "Acme", "n": 2}]}, []) is None


def test_a_full_run_answers_from_tools():
    out = asyncio.run(agent.answer(
        _Store(_records()), "d1", "which company appears most often?",
        _llm({"tool_calls": [{"tool": "value_counts",
                              "args": {"field": "company"}}]},
             _answer_payload())))
    assert out["intent"] == "query"
    assert "Acme" in out["answer"]
    assert out["chart"] is not None
    assert out["chart"]["items"][0]["n"] == 2
    tools = [t["tool"] for t in out["trace"]]
    assert "value_counts" in tools
    assert out["proposals"] == []


def test_a_change_request_never_reaches_the_model():
    seen = []

    async def _exploding(prompt, schema):
        seen.append(prompt)
        raise AssertionError("the model must not be called for a change request")

    out = asyncio.run(agent.answer(
        _Store(_records()), "d1", "only two sources instead of ten",
        _exploding))
    assert seen == []
    assert out["proposals"], "a change request must come back as a proposal"


def test_a_failed_plan_still_answers_from_real_measurements():
    async def _dead(prompt, schema):
        raise RuntimeError("provider down")

    # Planning dies, but composing is attempted with the default plan; make
    # composing fail too and the fallback answer must still cite measurements.
    out = asyncio.run(agent.answer(_Store(_records()), "d1", "what is here?", _dead))
    assert "Measured:" in out["answer"] or out["trace"], \
        "a double model failure must still report what the tools read"


def test_tool_args_are_clamped_not_trusted():
    ctx = {"records": _records(), "total": 3, "fields": ["company", "role"],
           "read": 3}
    out = agent.tool_value_counts(ctx, {"field": "company", "limit": 9999})
    assert len(out["values"]) <= 12
    with pytest.raises(Exception):
        agent.tool_search_records(ctx, {"q": ""})
