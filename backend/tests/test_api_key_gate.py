"""The API-key gate must fail closed.

The previous implementation returned early when `API_KEY` was unset, on the
reasoning that a missing key means "local dev". That inverts the failure mode:
a container started without a mounted .env became an open proxy that would spend
Tavily, Gemini, Groq and OpenRouter budget for anyone who found the URL, and
nothing reported it. These tests pin the corrected behaviour so it cannot
regress silently.

The rest of the suite is synchronous and `pytest-asyncio` is not a dependency,
so the coroutines are driven with `asyncio.run` rather than introducing async
test infrastructure for one file.
"""
import asyncio

import pytest

from app.api import deps
from app.core import config
from app.core.errors import AppError


def _settings(**overrides):
    base = {"API_KEY": "", "ALLOW_UNAUTHENTICATED": False}
    base.update(overrides)
    return config.Settings(**base)


def _gate(monkeypatch, *, key=None, **overrides):
    monkeypatch.setattr(deps, "settings", _settings(**overrides))
    return asyncio.run(deps.require_api_key(key))


def test_missing_key_and_no_opt_in_is_refused(monkeypatch):
    """The core regression: no key configured must NOT mean "allow"."""
    with pytest.raises(AppError) as exc:
        _gate(monkeypatch)
    assert exc.value.http == 503
    assert exc.value.code == "E_AUTH_NOT_CONFIGURED"


def test_explicit_opt_in_allows_anonymous(monkeypatch):
    """Open local dev is a recorded decision, not an accident."""
    _gate(monkeypatch, key=None, ALLOW_UNAUTHENTICATED=True)  # must not raise


def test_wrong_key_is_refused(monkeypatch):
    with pytest.raises(AppError) as exc:
        _gate(monkeypatch, key="wrong", API_KEY="correct-horse")
    assert exc.value.http == 401
    assert exc.value.code == "E_UNAUTHORIZED"


def test_correct_key_is_accepted(monkeypatch):
    _gate(monkeypatch, key="correct-horse", API_KEY="correct-horse")  # must not raise


def test_absent_key_against_configured_key_is_refused(monkeypatch):
    """A configured key means a key is required; None must not slip through."""
    with pytest.raises(AppError) as exc:
        _gate(monkeypatch, key=None, API_KEY="correct-horse")
    assert exc.value.http == 401


def test_opt_in_does_not_bypass_a_configured_key(monkeypatch):
    """ALLOW_UNAUTHENTICATED is only a fallback for a MISSING key. If a key is
    set it is enforced, otherwise the flag would be a silent bypass."""
    with pytest.raises(AppError) as exc:
        _gate(monkeypatch, key=None, API_KEY="correct-horse", ALLOW_UNAUTHENTICATED=True)
    assert exc.value.http == 401


def test_gate_is_constant_time_comparison(monkeypatch):
    """A `==` here would leak the key byte-by-byte through response timing."""
    import inspect

    src = inspect.getsource(deps.require_api_key)
    assert "compare_digest" in src
    assert "== expected" not in src


def test_auth_errors_use_the_app_error_envelope(monkeypatch):
    """The client reads `error.code`; a bare FastAPI `detail` is unparseable.

    This module was the only place in the app raising `HTTPException`, so a
    rejected key arrived in a different shape from every other failure.
    """
    with pytest.raises(AppError) as exc:
        _gate(monkeypatch)
    assert exc.value.envelope()["error"]["code"] == "E_AUTH_NOT_CONFIGURED"


def test_real_gated_route_refuses_without_a_key(monkeypatch):
    """Unit tests can prove the helper; this proves the wiring.

    `ALLOW_UNAUTHENTICATED` is forced off after the conftest autouse fixture has
    opened it, then a real request is made against a real route. Without this
    the suite could pass with the gate removed from the router entirely and
    nothing would notice — the unit tests only ever call `require_api_key`.
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "ALLOW_UNAUTHENTICATED", False)

    with TestClient(main_mod.app) as client:
        # No key configured at all: the deployment is misconfigured, and the
        # only honest answer is to refuse everything rather than serve open.
        monkeypatch.setattr(config_mod.settings, "API_KEY", "")
        misconfigured = client.get("/api/datasets")
        assert misconfigured.status_code == 503, misconfigured.text
        assert misconfigured.json()["error"]["code"] == "E_AUTH_NOT_CONFIGURED"

        # Key configured but not sent: refused, not silently served.
        monkeypatch.setattr(config_mod.settings, "API_KEY", "a-real-key")
        assert client.get("/api/datasets").status_code == 401
        assert client.get("/api/datasets",
                          headers={"X-API-Key": "nope"}).status_code == 401

        with_key = client.get("/api/datasets", headers={"X-API-Key": "a-real-key"})
        assert with_key.status_code == 200, with_key.text
