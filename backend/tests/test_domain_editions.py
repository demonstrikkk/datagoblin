"""The edition contract: discovery, the merge, and the seams it feeds.

The rule under test is that a vertical changes a plan additively and nothing
else. Where a test can prove "the core behaves as before when no edition is
asked for", it does -- a seam that changes default behaviour is a regression
wearing a feature's name.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.domain import api, registry
from app.domain.apply import apply_profile
from app.domain.profile import DomainProfile
from app.schemas.plan import FieldSpec
from app.services import planner, relevance, selectors


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EDITIONS_PATH", str(tmp_path / "editions"))
    registry.reset()
    yield
    registry.reset()


def _manifest(root: Path, name: str, **overrides) -> Path:
    directory = root / "editions" / name
    directory.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "version": "0.1.0", "api_version": 1,
            "description": f"{name} edition"}
    data.update(overrides)
    (directory / "edition.json").write_text(json.dumps(data), encoding="utf-8")
    return directory


def _plan(**overrides) -> dict:
    plan = {"goal": "patent cases", "entity": "case", "requested_count": 10,
            "max_results": 10,
            "fields": [{"name": "case_name", "type": "string",
                        "description": "Case name", "required": True}],
            "search_queries": ["patent cases"], "seed_domains": [],
            "seed_urls": [], "source_types": [],
            "traversal": {"max_pages_per_domain": 10}, "validation_rules": [],
            "dedupe_keys": ["case_name"], "allowed_sources": [], "max_pages": 12}
    plan.update(overrides)
    return plan


def _legal(**overrides) -> DomainProfile:
    data = {"name": "legal", "version": "0.2.0",
            "fields": [{"name": "citation", "type": "string",
                        "description": "Reporter citation"},
                       {"name": "court", "type": "string",
                        "description": "Court"}],
            "field_phrases": {"citation": "case citation"},
            "required_fields": ["citation"],
            "allowed_sources": ["courtlistener.com"],
            "seed_domains": ["courtlistener.com"],
            "relevance_terms": ["judgment"],
            "validation_rules": ["citation must be a reporter citation"],
            "extractor_guidance": "Citations are reporter citations, not URLs.",
            "planner_guidance": "A row is one judicial decision.",
            "examples": ["patent decisions from the Federal Circuit"]}
    data.update(overrides)
    return DomainProfile.model_validate(data)


def test_generic_is_built_in_and_conforms() -> None:
    names = [e["name"] for e in api.list_editions()]
    assert "generic" in names
    assert api.check_profile(api.generic_profile()) == []


def test_a_manifest_is_discovered_and_its_selectors_dir_is_absolute(tmp_path) -> None:
    directory = _manifest(tmp_path, "legal", selectors_dir="selectors")
    (directory / "selectors").mkdir()
    registry.reset()
    edition = registry.get("legal")
    assert edition is not None and edition.usable
    assert Path(edition.profile.selectors_dir) == (directory / "selectors").resolve()
    assert edition.source == "manifest"


def test_a_broken_manifest_is_reported_not_raised(tmp_path) -> None:
    directory = tmp_path / "editions" / "broken"
    directory.mkdir(parents=True)
    (directory / "edition.json").write_text("{not json", encoding="utf-8")
    registry.reset()
    edition = registry.get("broken")
    assert edition is not None and not edition.usable
    assert "invalid edition.json" in edition.error
    statuses = {e["name"]: e["status"] for e in api.list_editions()}
    assert statuses["broken"] == "error"


def test_an_edition_written_for_another_api_is_refused(tmp_path) -> None:
    _manifest(tmp_path, "future", api_version=99)
    registry.reset()
    edition = registry.get("future")
    assert edition is not None and not edition.usable
    assert "API 99" in edition.error
    with pytest.raises(AppError) as err:
        api.require("future")
    assert err.value.code == "E_VALIDATION"


def test_register_adds_an_in_process_edition() -> None:
    api.register(_legal())
    assert registry.get("legal").usable


def test_the_merge_is_additive() -> None:
    plan = _plan(allowed_sources=["user.example"],
                 validation_rules=["existing rule"],
                 seed_domains=["a.example"])
    out = apply_profile(plan, _legal())

    names = [f["name"] for f in out["fields"]]
    assert names == ["case_name", "citation", "court"]
    # A field the planner already declared is not replaced.
    assert out["fields"][0]["description"] == "Case name"
    # The edition's required field is required; the planner's identity survives.
    assert out["fields"][1]["required"] is True
    assert out["dedupe_keys"] == ["case_name"]
    # A user's allowlist wins; the edition's is recorded, not substituted.
    assert out["allowed_sources"] == ["user.example"]
    assert out["edition_allowed_sources"] == ["courtlistener.com"]
    assert out["seed_domains"] == ["a.example", "courtlistener.com"]
    assert out["validation_rules"] == ["existing rule",
                                       "citation must be a reporter citation"]
    assert out["edition"] == "legal"
    assert out["edition_version"] == "0.2.0"


def test_the_merge_is_idempotent() -> None:
    once = apply_profile(_plan(), _legal())
    twice = apply_profile(once, _legal())
    assert once == twice


def test_an_empty_allowlist_takes_the_editions() -> None:
    out = apply_profile(_plan(), _legal())
    assert out["allowed_sources"] == ["courtlistener.com"]


def test_compiling_with_an_edition_stamps_and_extends_the_plan(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPENCODE_STRICT", True)
    api.register(_legal())
    plan, provider = asyncio.run(
        planner.compile_plan("patent decisions from the federal circuit", None,
                             edition="legal"))
    assert provider
    assert plan["edition"] == "legal"
    assert plan["edition_version"] == "0.2.0"
    assert "citation" in [f["name"] for f in plan["fields"]]
    # The edition's curated allowlist survives the prompt's domain filter, which
    # would otherwise delete every domain the prompt did not literally name.
    assert "courtlistener.com" in plan["allowed_sources"]


def test_compiling_without_an_edition_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPENCODE_STRICT", True)
    plan, _ = asyncio.run(planner.compile_plan("patent decisions", None))
    assert plan.get("edition", "") == ""
    assert plan.get("edition_allowed_sources") is None


def test_an_unknown_edition_is_a_validation_error() -> None:
    with pytest.raises(AppError) as err:
        asyncio.run(planner.compile_plan("anything", None, edition="nope"))
    assert err.value.code == "E_VALIDATION"
    assert "installed" in err.value.message


def test_edition_guidance_reaches_the_planner_prompt(monkeypatch) -> None:
    api.register(_legal())
    seen: dict = {}

    async def _llm(prompt: str, schema: dict) -> dict:
        seen["prompt"] = prompt
        return {"data": _plan(goal="patent cases", entity="case")}

    asyncio.run(planner.compile_plan("patent cases", _llm, edition="legal"))
    assert "Domain rules for this edition: A row is one judicial decision." in seen["prompt"]


def test_an_edition_phrase_reaches_the_probe_query() -> None:
    plan = _plan(fields=[{"name": "docket_number", "type": "string"}],
                 dedupe_keys=["case_name"],
                 field_phrases={"docket_number": "docket number"})
    probes = planner._field_probe_queries(plan)
    assert any("docket number" in q for q in probes)
    assert planner._field_probe_queries(
        {**plan, "field_phrases": {}}) == []


def test_field_phrase_prefers_the_edition_over_the_core_table() -> None:
    assert planner.field_phrase("citation", {"citation": "case citation"}) == "case citation"
    assert planner.field_phrase("chief_executive") == "chief executive"


def test_relevance_terms_extend_the_entity_terms() -> None:
    plain = relevance.entity_terms({"entity": "court_decision"})
    extended = relevance.entity_terms({"entity": "court_decision",
                                       "relevance_terms": ["judgment"]})
    assert "judgment" in extended
    assert set(plain).issubset(set(extended))


def test_a_plan_can_point_at_an_editions_selectors(tmp_path) -> None:
    directory = _manifest(tmp_path, "legal", selectors_dir="selectors")
    schemas = directory / "selectors"
    schemas.mkdir()
    (schemas / "court.example.json").write_text(json.dumps({
        "domain": "court.example",
        "fields": {"case_name": {"selectors": ["h1.case"]}}}), encoding="utf-8")

    found = selectors.load_schema("court.example", str(schemas))
    assert found is not None and "case_name" in found["fields"]
    # The core directory is not searched for it: an edition's schema stays its own.
    assert selectors.load_schema("court.example") is None


def test_extractor_guidance_is_bounded_and_optional() -> None:
    from app.services.extractor import build_prompt

    fields = [{"name": "case_name", "type": "string", "required": True}]
    with_guidance = build_prompt(
        {"fields": fields, "extractor_guidance": "Cite the reporter, not the URL."},
        "https://court.example/1", "Case", "content")
    without = build_prompt({"fields": fields}, "https://court.example/1", "Case", "content")
    assert "Domain rules: Cite the reporter, not the URL." in with_guidance
    assert "Domain rules:" not in without


def test_conformance_names_the_mismatches() -> None:
    profile = _legal(required_fields=["case_number"], dedupe_keys=["docket"],
                     allowed_sources=["official court websites"],
                     selectors_dir="/definitely/not/here")
    problems = api.check_profile(profile)
    joined = " | ".join(problems)
    assert "case_number" in joined
    assert "docket" in joined
    assert "not a host" in joined
    assert "selectors_dir does not exist" in joined


def test_a_profile_without_anything_to_add_is_rejected_by_conformance() -> None:
    problems = api.check_profile(DomainProfile(name="empty"))
    assert any("cannot change what a plan does" in p for p in problems)
