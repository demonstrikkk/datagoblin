"""Hermetic tests: decision/LLM keys are always empty here.

Live credentials in .env must never affect unit tests (no network, no spend).
Tests that need provider behavior inject fakes explicitly. See docs/24.
"""
import pytest

#: The pristine `_jev_call`, stashed by `_judge_available` before it installs the
#: stub. Fixture order means a lazy capture inside `real_judge` would grab the
#: stub instead, so it has to be saved on the way past.
_REAL_JEV_CALL: list = []


@pytest.fixture
def real_judge(monkeypatch):
    """Undo the autouse judge stub, so `_jev_call` really runs.

    Needed by anything testing transport behaviour — throttling, retries, the
    free-then-paid rung order — which the default stub would otherwise hide.
    """
    from app.providers.decision import jev
    assert _REAL_JEV_CALL, "the real judge was not captured"
    monkeypatch.setattr(jev, "_jev_call", _REAL_JEV_CALL[0])


@pytest.fixture(autouse=True)
def _open_local_gate(monkeypatch):
    """Route tests exercise route logic; they do not carry an API key.

    The gate fails closed by default, so without this every authenticated route
    would answer 503 and the suite would test only the gate. `tests/
    test_api_key_gate.py` covers the fail-closed behaviour directly, including a
    real 503 on a real route, so the two concerns stay separate.

    Scoped per test and applied in place, matching how the autouse fixtures
    below reach `config_mod.settings`.
    """
    from app.core import config as config_mod
    monkeypatch.setattr(config_mod.settings, "ALLOW_UNAUTHENTICATED", True)

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

    if not _REAL_JEV_CALL:
        _REAL_JEV_CALL.append(jev._jev_call)

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
