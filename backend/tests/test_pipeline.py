"""DATAGOBLIN backend tests — pure logic only. No network, no keys, no live credits.

Covers: evidence guard, dedupe+conflict merge, normalizer, exporter injection guard,
SupervisorDecision budget cap, source triage, logging redaction, envelope invariant,
planner fallback + validation. Async tests via pytest.mark.asyncio (asyncio_mode=auto
not assumed — each async test is driven with asyncio.run).
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.decisions import SupervisorDecision  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.logging import _redact  # noqa: E402
from app.schemas.run import Envelope  # noqa: E402
from app.services import deduper as deduper_svc  # noqa: E402
from app.services import exporter as exporter_svc  # noqa: E402
from app.services import normalizer as normalizer_svc  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402
from app.services.source_router import FETCH_ORDER_MVP, triage_source  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# -- evidence guard ----------------------------------------------------------
def test_guard_rejects_unsupported_quote():
    v, st = run(validator_svc.verify_field("$15M", "raised $15 million",
                                           "acme raised $12m seed", True, "string"))
    assert (v, st) == (None, "unverified")


def test_guard_accepts_verbatim_quote():
    v, st = run(validator_svc.verify_field("$12.5M", "raised $12.5 million",
                                           "Acme raised $12.5 MILLION series A", True, "string"))
    assert st == "verified" and v == "$12.5M"


def test_guard_empty_value_is_unverified():
    assert run(validator_svc.verify_field("", "q", "text q", True, "string")) == (None, "unverified")
    assert run(validator_svc.verify_field(None, "q", "text q", False, "string")) == (None, "unverified")


def test_screening_plural_tolerant(monkeypatch):
    from app.providers.decision import jev as jev_mod
    assert jev_mod._stem_variants("company") >= {"company", "companies"}

    async def _none(state, questions):
        return None  # force deterministic fallback (no key path)

    monkeypatch.setattr(jev_mod, "_jev_call", _none)
    r = run(jev_mod.source_screening(
        "https://x.example/a", "Top 10 Companies in India",
        "Listed companies by market capitalization", "company"))
    assert r["judgment"] == "YES" and r["provider"] == "deterministic"
    r2 = run(jev_mod.source_screening(
        "https://x.example/b", "Sourdough recipes", "baking bread", "company"))
    assert r2["judgment"] == "NO"


# -- wrap_record --------------------------------------------------------------
def test_wrap_record_shapes_provenance():
    raw = {"fields": {"company_name": "Acme AI"},
           "evidence": [{"field": "company_name", "quote": "Acme AI raises",
                         "source_url": "https://x.example/a"}]}
    wrapped = run(validator_svc.wrap_record(
        [{"name": "company_name", "type": "string", "description": "", "required": True}],
        raw, "Acme AI raises seed round today", "https://x.example/a", "T"))
    cell = wrapped["company_name"]
    assert cell["verification_status"] == "verified"
    assert cell["source"]["url"] == "https://x.example/a"
    assert cell["source"]["retrieved_at"] != ""


# -- dedupe -------------------------------------------------------------------
def _row(name):
    return {"fields": {"company_name": {"value": name, "verification_status": "verified",
                                        "source": {"url": "u", "title": "t", "quote": name,
                                                   "retrieved_at": "now"}}}}


def test_dedupe_exact_and_fuzzy_merge():
    rows = [_row("Acme AI Ltd."), _row("acme ai"), _row("Nova Labs")]
    canon, merged = deduper_svc.dedupe_records(rows, ["company_name"])
    assert len(canon) == 2 and merged == 1


def test_dedupe_conflict_marks_not_merges():
    rows = [_row("Acme AI"), _row("Acme Artificial Intelligence")]
    rows[1]["fields"]["company_name"]["value"] = "Acme Bio"
    canon, merged = deduper_svc.dedupe_records(rows, ["company_name"], threshold=99.0)
    assert len(canon) == 2 and merged == 0  # high threshold: no fuzzy merge


# -- normalizer ----------------------------------------------------------------
def test_normalize_currency_variants():
    for raw, amount in [("$12.5 million", 12500000), ("$12.5M", 12500000),
                        ("USD 12,500,000", 12500000)]:
        out = normalizer_svc.normalize_currency(raw)
        assert out["amount"] == amount and out["original"] == raw, raw


def test_normalize_record_preserves_original():
    rec = normalizer_svc.normalize_record({"funding": {"value": "$12.5M"}})
    assert rec["funding"]["value"] == "$12.5M"
    assert rec["funding"]["normalized"]["amount"] == 12500000


# -- exporter ------------------------------------------------------------------
def test_exporter_csv_injection_guard():
    schema = [{"name": "company_name"}]
    rows = [{"fields": {"company_name": {"value": "=HYPERLINK(\"evil\")",
                                         "verification_status": "verified",
                                         "source": {"url": "u", "retrieved_at": "t"}}}}]
    content = exporter_svc.to_csv(schema, rows)
    assert "'=HYPERLINK" in content


def test_exporter_rejects_bad_format_and_empty():
    with pytest.raises(AppError):
        exporter_svc.export_dataset([], [], "xml")
    with pytest.raises(AppError):
        exporter_svc.export_dataset([{"name": "a"}], [], "json")


# -- supervisor decision --------------------------------------------------------
def test_decision_budget_cap_forces_fetch():
    dec = SupervisorDecision(decision="REFINE_SEARCH", next_queries=["a", "b", "c", "d"])
    capped = dec.validated({"max_queries": 2, "used_queries": ["x", "y"]})
    assert capped.next_queries == [] and capped.decision == "FETCH"


def test_decision_trims_to_three():
    dec = SupervisorDecision(decision="REFINE_SEARCH", next_queries=["1", "2", "3", "4"])
    assert len(dec.validated({"max_queries": 8, "used_queries": []}).next_queries) == 3


# -- triage ----------------------------------------------------------------------
def test_triage_routes():
    assert triage_source("https://x.example/article") == "web:http"
    assert triage_source("https://x.example/a.pdf") == "document:docling"
    assert triage_source("https://x.example/a", llm_hint="js-heavy") == "web:crawl4ai"
    assert "web:http" in FETCH_ORDER_MVP and "web:crawl4ai" in FETCH_ORDER_MVP


def test_triage_rejects_bad_scheme_and_private_fetch():
    from app.providers.crawl.fetcher import guard_url
    with pytest.raises(AppError):
        triage_source("ftp://x.example/f")
    with pytest.raises(AppError):
        triage_source("not-a-url")
    # Private hosts pass triage (no DNS there) but are refused at fetch time.
    assert triage_source("http://localhost:8000/x") == "web:http"
    with pytest.raises(AppError):
        guard_url("http://localhost:8000/x")


# -- logging redaction --------------------------------------------------------------
def test_redact_nested_secrets():
    payload = {"api_key": "s", "nested": {"SUPABASE_KEY": "s", "ok": 1}, "list": [{"token": "s"}]}
    out = _redact(payload)
    assert out == {"api_key": "***", "nested": {"SUPABASE_KEY": "***", "ok": 1},
                   "list": [{"token": "***"}]}


# -- envelope invariant -----------------------------------------------------------------
def test_envelope_requires_exactly_one():
    with pytest.raises(Exception):
        Envelope(data={"a": 1}, error={"code": "x"})
    with pytest.raises(Exception):
        Envelope(data=None, error=None)
    assert Envelope(data={"a": 1}).data == {"a": 1}


# -- planner -----------------------------------------------------------------------
def test_planner_rejects_empty_prompt():
    with pytest.raises(AppError):
        run(planner_svc.compile_plan("   ", llm=None))


def test_planner_fallback_marked():
    plan, provider = run(planner_svc.compile_plan("Find 15 AI startups", llm=None))
    assert provider == "rule-based" and plan["fields"][0]["name"] == "company_name"
    planner_svc.coerce_plan(plan)  # rule output satisfies the same contract as LLM plans


def test_planner_generic_fallback_conforms():
    planner_svc.coerce_plan(planner_svc._fallback_plan("anything"))
