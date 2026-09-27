"""Hermetic tests: decision/LLM keys are always empty here.

Live credentials in .env must never affect unit tests (no network, no spend).
Tests that need provider behavior inject fakes explicitly. See docs/24.
"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def _no_env_file():
    """Rebuild settings from defaults + process env, ignoring .env.

    Settings reads `.env` relative to the CWD, so running pytest from the repo
    root (the documented way to start the server) pulled the developer's real
    .env into the suite — OPENCODE_STRICT=true alone flipped the LLM chain from
    "cascade" to "strict" and failed the cascade test. Higher scope runs first,
    and the reset is IN PLACE so every module holding the settings object sees
    it. Keys are blanked per test by _no_provider_keys below.
    """
    from app.core.config import Settings, settings
    clean = Settings(_env_file=None)
    for name in Settings.model_fields:
        setattr(settings, name, getattr(clean, name))


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("ZEN_API_KEY", "")
    from app.core import config as config_mod
    monkeypatch.setattr(config_mod.settings, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "")


@pytest.fixture(autouse=True)
def _judge_available(monkeypatch):
    """A judge that says SUPPORTED, by default, for every test.

    This exists because the suite was passing for the wrong reason. With no
    judge reachable, `evidence_verification` used to return SUPPORTED, so every
    substring-matching field was reported `verified` and no test ever noticed
    that nothing had actually judged it. Now an unreachable judge yields
    `judgment_unavailable`, so tests that mean "the judge supported this" have
    to say so. A test that wants the unavailable path opts out with
    `no_judge`.
    """
    from app.providers.decision import jev

    async def _supported(prompt, schema, **kw):
        key = next(iter(schema), "support")
        return {key: {"type": "noul", "noul": 0.9}}

    monkeypatch.setattr(jev, "_jev_call", _supported)


@pytest.fixture
def no_judge(monkeypatch):
    """Opt out: no judge is reachable, as in a real outage."""
    from app.providers.decision import jev

    async def _none(prompt, schema, **kw):
        return None

    monkeypatch.setattr(jev, "_jev_call", _none)
