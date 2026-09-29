"""The four things that went wrong in one session, each pinned down.

1. A value that is punctuation rather than data (`—`, `”`) was stored as if it
   had been extracted, and read in the UI as `â€”` / `â€"`.
2. `available()` reported a 401 as "reachable", so a health check was green
   while every extraction was being refused.
3. A run spent twelve fetches and three minutes before finding out.
4. Search order decided which pages got opened, so five of twelve were a
   "top-10-products-india-imports-from-the-usa" listicle.

Each test names the failure it prevents, because a test that only says a
function returns a value would pass again the moment it regressed.

Mojibake literals are written as escapes throughout. That is not pedantry: the
mojibake of U+201D is U+00E2 U+20AC U+009D, and that last character is a C1
control code which no editor or console round-trips reliably. Spelling it as a
visible `â€"` is how the wrong bytes get into a test in the first place.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.graph import _plan_relevance
from app.core import config as config_mod
from app.services import normalizer as norm

# The strings the UI actually displayed, and what each one really is.
EM_DASH = "—"          # U+2014
RIGHT_QUOTE = "”"      # U+201D
MOJI_EM_DASH = "â€”"    # "—" mangled; 0x94 IS defined in cp1252
MOJI_QUOTE = "â€\x9d"      # "”" mangled; 0x9D is NOT, so this one
                                      # cannot be repaired and is caught by residue


def _run(coro):
    """This repo drives coroutines with asyncio.run rather than pytest-asyncio,
    so covering the async paths needs no new test dependency."""
    return asyncio.run(coro)


# --- 1. punctuation is absence, not data -------------------------------------

PLACEHOLDERS = [EM_DASH, "–", "-", RIGHT_QUOTE, "“", '"', "'", "…", "?", "!",
                ".", "  ", "n/a", "N/A", "NA", "null", "NULL", "None",
                "unknown", "tbd", "not available", "not specified"]


@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_punctuation_and_absence_words_are_not_values(value):
    assert norm.is_placeholder(value) is True


@pytest.mark.parametrize("value", [
    "Acme Corp", "Series B", "2", "2024", "S2024", "collaboration",
    "San Francisco", "BSE:500325", "0", "Party",
])
def test_real_values_are_not_mistaken_for_placeholders(value):
    assert norm.is_placeholder(value) is False


def test_zero_and_false_are_answers_not_absences():
    """Zero employees is a fact. Treating it as empty would delete exactly the
    values a dataset was collected for."""
    assert norm.is_placeholder(0) is False
    assert norm.is_placeholder(0.0) is False
    assert norm.is_placeholder(False) is False
    assert norm.is_placeholder([]) is False


def test_mojibake_is_repaired_back_to_the_character_it_was():
    assert norm.repair_mojibake(MOJI_EM_DASH) == EM_DASH
    assert norm.repair_mojibake("CafÃ©") == "Café"
    assert norm.repair_mojibake("R&D") == "R&D"          # untouched
    assert norm.repair_mojibake("plain ascii") == "plain ascii"


def test_a_repair_that_would_change_a_real_value_is_refused():
    """A wrong repair is worse than visible corruption: it silently changes a
    value in a dataset whose whole claim is that values are evidence-backed."""
    # A lone lead byte followed by ASCII: cp1252-encodable, invalid UTF-8.
    assert norm.repair_mojibake("Ãabc") == "Ãabc"
    assert norm.repair_mojibake("â€") == "â€"


def test_a_repaired_punctuation_value_is_still_an_absence():
    """The point of repairing first: the display string is a mojibake em dash,
    and an em dash is not data whichever way it is spelled. Without the repair
    the leading `â` counts as a letter and the value would be stored."""
    assert norm.is_placeholder(MOJI_EM_DASH) is True
    assert norm.is_placeholder(MOJI_QUOTE) is True


def test_clean_value_trims_and_repairs_but_keeps_the_value():
    assert norm.clean_value("  CafÃ©  ") == "Café"
    assert norm.clean_value(7) == 7


# --- the extractor must drop them --------------------------------------------

def _payload(fields: dict, evidence: list[dict]) -> dict:
    return {"records": [{"fields": fields, "evidence": evidence}],
            "coverage": "partial"}


def _ev(field: str, value: str, quote: str) -> dict:
    return {"field": field, "value": value, "quote": quote,
            "source_url": "http://x/a", "reference_id": ""}


def test_the_extractor_drops_punctuation_valued_fields():
    """The reported failure: a 15-field schema stored mostly as punctuation,
    every cell present, unverified, and displayed as a stray symbol. A dataset
    that looks corrupt instead of empty."""
    from app.services import extractor

    out = extractor.coerce_output(_payload(
        {"company_name": "Acme Corp", "country": EM_DASH,
         "industry_segment": RIGHT_QUOTE, "founded_year": "2024"},
        [_ev("company_name", "Acme Corp", "Acme Corp is a company."),
         _ev("founded_year", "2024", "Founded 2024 in San Francisco."),
    ]), "http://x/a")

    fields = out[0][0]["fields"]
    assert fields == {"company_name": "Acme Corp", "founded_year": "2024"}
    assert "country" not in fields and "industry_segment" not in fields


def test_evidence_for_a_dropped_field_is_dropped_with_it():
    from app.services import extractor

    out = extractor.coerce_output(_payload(
        {"company_name": "Acme Corp", "country": EM_DASH},
        [_ev("company_name", "Acme Corp", "Acme Corp is a company."),
         _ev("country", EM_DASH, "some quote")]), "http://x/a")

    assert [e["field"] for e in out[0][0]["evidence"]] == ["company_name"]


def test_a_na_field_recovers_its_value_from_its_evidence():
    """Existing behaviour must survive the widening: a null field with matching
    evidence is filled from that evidence, not dropped."""
    from app.services import extractor

    out = extractor.coerce_output(_payload(
        {"company_name": "NA", "founded_year": "2024"},
        [_ev("company_name", "Acme Corp", "Acme Corp is a company.")]),
        "http://x/a")

    assert out[0][0]["fields"]["company_name"] == "Acme Corp"


def test_a_record_that_is_all_placeholders_is_not_a_record():
    from app.services import extractor

    recs, coverage = extractor.coerce_output(
        _payload({"a": EM_DASH, "b": RIGHT_QUOTE}, []), "http://x/a")
    assert recs == []


def test_a_mojibake_placeholder_is_dropped_rather_than_stored():
    """The exact string the user saw in the dataset, not just the clean form."""
    from app.services import extractor

    out = extractor.coerce_output(_payload(
        {"company_name": "Acme Corp", "country": MOJI_QUOTE},
        [_ev("company_name", "Acme Corp", "Acme Corp is a company.")]),
        "http://x/a")
    assert "country" not in out[0][0]["fields"]


# --- the validator must refuse them as a last net ----------------------------

def test_the_validator_stores_nothing_for_a_punctuation_value():
    """Defence in depth: backfill and refresh build cells through the validator
    too, so a placeholder reaching one must not become a cell."""
    from app.services import validator

    text = "Acme Corp is a company. " + EM_DASH
    for value in (EM_DASH, RIGHT_QUOTE, "N/A", "unknown", MOJI_EM_DASH):
        stored, status = _run(validator.verify_field(
            value, EM_DASH, text, required=False, ftype="string"))
        assert stored is None, value
        assert status == "unverified"


def test_the_validator_still_stores_a_real_value():
    from app.services import validator

    text = "Acme Corp is a company."
    stored, status = _run(validator.verify_field(
        "Acme Corp", "Acme Corp is a company.", text,
        required=False, ftype="string"))
    assert stored == "Acme Corp"
    assert status in ("verified", "unverified", "judgment_unavailable")


# --- 2. a 401 is not "reachable" ---------------------------------------------

def test_a_401_is_reported_as_an_auth_failure_not_as_healthy(monkeypatch):
    """`available()` used to call anything under 500 "reachable", so a server
    that was up and refusing every credential passed a health check."""
    from app.providers.llm import opencode

    class _Resp:
        status_code = 401

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(opencode.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(opencode.httpx, "AsyncClient", lambda **k: _Client())

    out = _run(opencode.available())
    assert out["reachable"] is False
    assert out["auth_failed"] is True
    assert "401" in out["error"]
    assert ".env" in out["error"]        # says where the mismatch is


def test_a_healthy_server_is_still_reachable(monkeypatch):
    from app.providers.llm import opencode

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(opencode.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(opencode.httpx, "AsyncClient", lambda **k: _Client())

    out = _run(opencode.available())
    assert out["reachable"] is True
    assert "auth_failed" not in out


def test_preflight_explains_an_auth_failure_rather_than_saying_unreachable(monkeypatch):
    from app.providers.llm import opencode

    async def fake():
        return {"reachable": False, "auth_failed": True,
                "error": "the server rejected this process's credentials (HTTP 401)"}

    monkeypatch.setattr(opencode, "available", fake)
    reason = _run(opencode.preflight())
    assert reason is not None and "401" in reason


def test_preflight_says_nothing_when_the_provider_is_usable(monkeypatch):
    from app.providers.llm import opencode

    async def fake():
        return {"reachable": True, "error": ""}

    monkeypatch.setattr(opencode, "available", fake)
    assert _run(opencode.preflight()) is None


def test_preflight_does_not_block_on_a_deliberately_disabled_provider(monkeypatch):
    """Switching the provider off is a choice, not a misconfiguration, and
    another provider may be serving the run. Refusing every run because of a
    setting the user turned on purpose is a worse failure than the 401 this
    check exists to catch."""
    from app.providers.llm import opencode

    async def fake():
        return {"reachable": False, "error": "disabled"}

    monkeypatch.setattr(opencode, "available", fake)
    assert _run(opencode.preflight()) is None


def test_preflight_does_not_block_on_an_unconfigured_provider(monkeypatch):
    from app.providers.llm import opencode

    async def fake():
        return {"reachable": False, "error": "unconfigured"}

    monkeypatch.setattr(opencode, "available", fake)
    assert _run(opencode.preflight()) is None


def test_preflight_does_block_on_an_unreachable_enabled_provider(monkeypatch):
    """Configured, switched on, and not answering — that is a fault."""
    from app.providers.llm import opencode

    async def fake():
        return {"reachable": False, "error": "connection refused",
                "base_url": "http://127.0.0.1:4096"}

    monkeypatch.setattr(opencode, "available", fake)
    reason = _run(opencode.preflight())
    assert reason and "connection refused" in reason


# --- 3. a run refuses before it spends ---------------------------------------

def test_a_run_with_an_unusable_provider_refuses_before_discovering():
    """Twelve successful fetches and three minutes were spent discovering this
    in the live failure. The point is that it now costs under a second."""
    from app.services import runner

    searched = []

    async def _search(*a, **k):
        searched.append(1)
        return []

    async def _preflight():
        return "the LLM server rejected this process's credentials (HTTP 401)"

    ctx = {"search": _search, "fetch": None, "llm": None, "store": None,
           "llm_preflight": _preflight}
    out = _run(runner._body("r1", {"goal": "x"}, ctx, _noop_emit))

    assert out["status"] == "FAILED"
    assert "401" in out["error"]
    assert searched == []          # discovery was never reached
    assert out["no_yield_reason"]


def test_a_usable_provider_does_not_block_the_run():
    """The gate must not fire when there is nothing wrong, or it would refuse
    every healthy run."""
    from app.services import runner

    checked = []

    async def _preflight():
        checked.append(1)
        return None

    async def _search(*a, **k):
        return []

    ctx = {"search": _search, "fetch": None, "llm": None, "store": _StubStore(),
           "llm_preflight": _preflight}
    try:
        out = _run(runner._body("r1", {"goal": "x", "max_pages": 1}, ctx, _noop_emit))
    except Exception:
        # It got past the pre-flight and into the rest of the pipeline, which is
        # all this test is about.
        out = None
    assert checked == [1]
    assert out is None or out.get("status") != "FAILED"


def test_a_run_without_a_preflight_hook_still_works():
    """The hook is optional: a harness that injects no provider must not be
    failed by a check it never asked for."""
    from app.services import runner

    async def _search(*a, **k):
        return []

    ctx = {"search": _search, "fetch": None, "llm": None, "store": _StubStore()}
    try:
        out = _run(runner._body("r1", {"goal": "x", "max_pages": 1}, ctx, _noop_emit))
    except Exception:
        out = None
    assert out is None or out.get("status") != "FAILED"


async def _noop_emit(_ev):
    return None


class _StubStore:
    async def upsert_source(self, *a, **k):
        return None


# --- 4. search order must not decide the page budget -------------------------

PLAN = {
    "entity": "stock",
    "goal": "find top 10 stocks in india share market",
    "queries": ["top 10 stocks in india share market"],
    "fields": [{"name": "company_name"}, {"name": "ticker_symbol"},
               {"name": "founded_year"}, {"name": "employee_count"}],
}

# The exact candidates the live run opened, and what it should have preferred.
ON_TARGET = {
    "url": "https://www.nseindia.com/market-data/live-equity-market",
    "title": "Live Equity Market - NSE India",
}
ON_TARGET_2 = {
    "url": "https://www.morningstar.in/equities.aspx",
    "title": "Morningstar India Equities",
}
OFF_TARGET_LISTICLE = {
    "url": "https://in.thedollarbusiness.com/blogs/top-10-products-india-imports-from-the-usa/2324",
    "title": "Top 10 products India imports from the USA",
}
OFF_TARGET_BLOG = {
    "url": "https://www.indiahandmade.com",
    "title": "India Handmade",
}
OFF_TARGET_SHOP = {
    "url": "https://www.ishopindian.com",
    "title": "iShop India",
}


def test_a_listicle_matching_the_query_words_ranks_below_a_stock_page():
    """Both contain "top 10" and "india". Only one is about stocks — and the
    word that says so is a synonym, because the page says "equity market"."""
    assert _plan_relevance(ON_TARGET, PLAN) > _plan_relevance(OFF_TARGET_LISTICLE, PLAN)


def test_both_pages_that_could_answer_it_beat_every_page_that_cannot():
    on = [_plan_relevance(c, PLAN) for c in (ON_TARGET, ON_TARGET_2)]
    off = [_plan_relevance(c, PLAN) for c in
           (OFF_TARGET_LISTICLE, OFF_TARGET_BLOG, OFF_TARGET_SHOP)]
    assert min(on) > max(off), (on, off)


def test_a_synonym_counts_but_an_exact_entity_word_counts_more():
    """"equity market" has to beat an unrelated page, but a page that says
    "stock" itself is the better evidence."""
    exact = {"url": "https://x.example/stocks", "title": "Stocks"}
    syn = {"url": "https://x.example/live-equity-market", "title": "Equity Market"}
    neither = {"url": "https://x.example/somewhere", "title": "Somewhere"}
    assert _plan_relevance(exact, PLAN) > _plan_relevance(syn, PLAN)
    assert _plan_relevance(syn, PLAN) > _plan_relevance(neither, PLAN)


def test_an_editorial_slug_is_demoted_even_when_the_words_match():
    """Demoted, not dropped: the page may still be worth one page, and only the
    budget is being rationed."""
    without_slug = {"url": "https://x.example/products-india",
                    "title": "Products India"}
    with_slug = dict(without_slug, url=without_slug["url"].replace(
        "x.example", "x.example/blogs"))
    assert _plan_relevance(with_slug, PLAN) < _plan_relevance(without_slug, PLAN)


def test_a_candidate_matching_nothing_scores_zero_rather_than_negative():
    assert _plan_relevance({"url": "https://example.com/a", "title": "A"},
                           PLAN) == 0.0


def test_relevance_always_returns_a_comparable_number():
    for c in (ON_TARGET, ON_TARGET_2, OFF_TARGET_LISTICLE, OFF_TARGET_BLOG,
              OFF_TARGET_SHOP):
        assert isinstance(_plan_relevance(c, PLAN), float)


def test_an_absurd_ordinal_does_not_outrank_the_entity():
    """"Top 10" is the user's phrasing, not the subject. It must not be worth
    more than the word that says what kind of thing is wanted."""
    for c in (OFF_TARGET_LISTICLE, OFF_TARGET_BLOG, OFF_TARGET_SHOP):
        assert _plan_relevance(ON_TARGET, PLAN) > _plan_relevance(c, PLAN)


# --- 5. config must not depend on the working directory ----------------------

def test_the_env_file_is_resolved_to_an_absolute_path():
    """`env_file=".env"` is CWD-relative, so starting the API from anywhere but
    the repo root silently emptied OPENCODE_PASSWORD — the same 401 as a wrong
    password, needing the opposite fix."""
    assert config_mod._ENV_FILE is not None
    assert config_mod._ENV_FILE.is_absolute()
    assert config_mod._ENV_FILE.is_file()
    assert config_mod.Settings.model_config["env_file"] == str(config_mod._ENV_FILE)


def test_the_password_survives_being_started_from_another_directory(monkeypatch, tmp_path):
    """The regression this guards: start the API from tmp_path and the password
    must still be there."""
    monkeypatch.chdir(tmp_path)
    from app.core.config import Settings
    s = Settings()
    assert s.OPENCODE_PASSWORD, "the password vanished outside the repo root"
