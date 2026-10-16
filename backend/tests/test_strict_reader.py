"""Under `OPENCODE_STRICT`, an unusable reader is a refusal, not a shrug.

`structured_generate` returns `opencode_structured` directly when strict mode is
on — there is no Groq/Gemini cascade to fall into. So a run whose local reader
is down cannot extract anything, and the only two possible outcomes are:

  * refuse in under a second, before a single Tavily query is spent; or
  * report a completed run with zero records.

The second is what happened. `preflight()` treated an unusable primary as
tolerable because "another provider may serve this run" — true in loose mode,
false by definition in strict mode. These tests pin the fail-closed behaviour and
keep the loose mode from regressing into being equally fatal.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.providers.llm import opencode


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = {
        "OPENCODE_STRICT": settings.OPENCODE_STRICT,
        "OPENCODE_ENABLED": settings.OPENCODE_ENABLED,
        "OPENCODE_BASE_URL": settings.OPENCODE_BASE_URL,
    }
    yield
    for key, value in saved.items():
        setattr(settings, key, value)


def _stub_state(monkeypatch, **state):
    base = {"reachable": False, "base_url": "http://127.0.0.1:4096",
            "enabled": True, "error": "unreachable"}
    base.update(state)

    async def fake_available():
        return dict(base)

    monkeypatch.setattr(opencode, "available", fake_available)


# ------------------------------------------------------------------ strict mode


def test_strict_refuses_an_unreachable_reader(monkeypatch):
    _stub_state(monkeypatch, error="All connection attempts failed")
    settings.OPENCODE_STRICT = True
    reason = run(opencode.preflight())
    assert reason, "strict mode with a dead reader must refuse the run"
    assert "OPENCODE_STRICT" in reason, (
        "the refusal must say why there is no fallback, or the operator looks "
        "for a cloud provider that strict mode has switched off")


def test_strict_refuses_a_disabled_reader(monkeypatch):
    """The case that used to be waved through.

    `disabled` meant "the user turned it off on purpose, another provider will
    serve the run" — which strict mode forbids outright.
    """
    _stub_state(monkeypatch, error="disabled", enabled=False)
    settings.OPENCODE_STRICT = True
    reason = run(opencode.preflight())
    assert reason, (
        "strict mode with no reader configured must refuse; otherwise the run "
        "searches, crawls, extracts nothing and reports success")
    assert "OPENCODE_ENABLED" in reason


def test_strict_refuses_an_unconfigured_reader(monkeypatch):
    _stub_state(monkeypatch, error="unconfigured", base_url="")
    settings.OPENCODE_STRICT = True
    reason = run(opencode.preflight())
    assert reason
    assert "OPENCODE_BASE_URL" in reason


def test_strict_still_refuses_on_auth_failure(monkeypatch):
    _stub_state(monkeypatch, error="bad password", auth_failed=True)
    settings.OPENCODE_STRICT = True
    reason = run(opencode.preflight())
    assert reason
    assert "credential" in reason.lower() or "password" in reason.lower()


def test_strict_allows_a_healthy_reader(monkeypatch):
    _stub_state(monkeypatch, reachable=True)
    settings.OPENCODE_STRICT = True
    assert run(opencode.preflight()) is None


# ------------------------------------------------------------------- loose mode


@pytest.mark.parametrize("state", [
    {"error": "disabled", "enabled": False},
    {"error": "unconfigured", "base_url": ""},
])
def test_loose_mode_tolerates_a_deliberately_disabled_primary(monkeypatch, state):
    """A provider the user switched off is a choice, not a fault.

    A cloud fallback may serve this run, and refusing every run because of a
    setting the user turned on purpose is worse than falling back.
    """
    _stub_state(monkeypatch, **state)
    settings.OPENCODE_STRICT = False
    assert run(opencode.preflight()) is None


@pytest.mark.parametrize("state,expected", [
    ({"error": "connection refused"}, "connection refused"),
    ({"error": "the server rejected this process's credentials (HTTP 401)",
      "auth_failed": True}, "401"),
])
def test_loose_mode_still_reports_an_unusable_primary(monkeypatch, state, expected):
    """Switched off is tolerated; *broken* is not, even with a fallback.

    A cloud fallback is only a fallback if a cloud key is configured, and a run
    that quietly starts paying a different provider is exactly what this check
    exists to surface.
    """
    _stub_state(monkeypatch, **state)
    settings.OPENCODE_STRICT = False
    reason = run(opencode.preflight())
    assert reason, (
        "an unreachable or unauthorised primary must be reported in loose mode "
        "too; only a deliberately disabled one is tolerated")
    assert expected in reason, (
        f"the reason must carry the underlying error, got: {reason}")


def test_loose_mode_does_not_claim_there_is_no_fallback(monkeypatch):
    """The refusal text is only ever shown in strict mode."""
    _stub_state(monkeypatch, error="unreachable")
    settings.OPENCODE_STRICT = False
    reason = run(opencode.preflight())
    assert reason
    assert "no fallback provider" not in reason


# ------------------------------------------------------------- the explanation


@pytest.mark.parametrize("state,expected", [
    ({"error": "disabled"}, "OPENCODE_ENABLED"),
    ({"error": "unconfigured"}, "OPENCODE_BASE_URL"),
    ({"error": "unreachable"}, "opencode serve"),
    ({"error": "bad password", "auth_failed": True}, "OPENCODE_PASSWORD"),
])
def test_the_explanation_names_the_actual_problem(state, expected):
    text = opencode.explain_unusable(state)
    assert expected in text, f"explanation does not mention {expected}: {text}"
    assert len(text) > 20, "an explanation must say something"


def test_the_explanation_is_shared_by_the_script_and_the_runtime():
    """`scripts/preflight.py` imports this function rather than restating it."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "preflight.py")
    text = src.read_text(encoding="utf-8")
    assert "explain_unusable" in text
    assert "OPENCODE_STRICT" in text, (
        "the script must branch on strict mode the same way the runtime does")


# --------------------------------------------------------- the cascade contract


def test_strict_mode_really_has_no_cascade(monkeypatch):
    """The premise of every test above. If a cascade exists, they are wrong."""
    from app.providers.llm import generate as llm_generate

    called: list[str] = []

    async def fake_opencode(prompt, schema):
        called.append("opencode")
        raise RuntimeError("opencode down")

    async def fake_groq(prompt, schema):
        called.append("groq")
        return {"data": {}, "provider": "groq"}

    async def fake_gemini(prompt, schema):
        called.append("gemini")
        return {"data": {}, "provider": "gemini"}

    monkeypatch.setattr(llm_generate, "opencode_structured", fake_opencode)
    monkeypatch.setattr(llm_generate, "groq_structured", fake_groq)
    monkeypatch.setattr(llm_generate, "gemini_structured", fake_gemini)

    settings.OPENCODE_STRICT = True
    try:
        asyncio.run(llm_generate.structured_generate("p", {}))
    except Exception:  # noqa: BLE001
        pass
    assert called == ["opencode"], (
        f"strict mode reached a fallback provider ({called}); either the "
        f"cascade or these tests need updating")