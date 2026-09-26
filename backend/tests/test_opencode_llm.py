"""OpenCode-primary LLM chain tests (session protocol). Hermetic: no network."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.providers.llm import generate as gen  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)[:300]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    seen: dict = {}
    mode: str = "ok"
    answer: str = '{"records": [], "coverage": "none"}'

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Client.seen.setdefault("posts", []).append({"url": url, "json": json,
                                                     "headers": headers})
        if isinstance(_Client.next_post, Exception):
            raise _Client.next_post
        return _Client.next_post

    async def get(self, url, params=None, headers=None):
        _Client.seen["get"] = {"url": url, "params": params}
        if _Client.mode == "prose":
            text = "Sorry, here is prose without any JSON object in it at all"
        else:
            text = _Client.answer
        return _Resp(200, {"data": [
            {"id": "msg_1", "type": "assistant",
             "content": [{"type": "text", "text": text}]}]})


@pytest.fixture(autouse=True)
def _opencode_cfg(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_BASE_URL",
                        "http://127.0.0.1:4096")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_PASSWORD", "pw")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_MODEL", "test-model")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_TIMEOUT_S", 30)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    _Client.seen = {}
    _Client.mode = "ok"
    _Client.next_post = _Resp(200, {"data": {"id": "ses_1"}})
    yield


def _session_ok():
    _Client.next_post = _Resp(200, {"data": {"id": "ses_1"}})


def test_opencode_success_session_flow():
    _session_ok()
    out = run(gen.opencode_structured("Extract.", {"type": "object"}))
    assert out["provider"] == "opencode" and out["model"] == "test-model"
    assert out["data"] == {"records": [], "coverage": "none"}
    posts = _Client.seen["posts"]
    assert posts[0]["url"] == "http://127.0.0.1:4096/api/session"
    assert posts[1]["url"] == "http://127.0.0.1:4096/api/session/ses_1/model"
    assert posts[2]["url"] == "http://127.0.0.1:4096/api/session/ses_1/prompt"
    assert "JSON object only" in posts[2]["json"]["prompt"]["text"]
    assert posts[2]["headers"]["Authorization"].startswith("Basic ")
    assert _Client.seen["get"]["url"].endswith("/api/session/ses_1/message")


def test_opencode_401_fatal():
    _Client.next_post = _Resp(401, {})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_FATAL"


def test_opencode_down_transient():
    import httpx as httpx_mod
    _Client.next_post = httpx_mod.ConnectError("refused")
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_prose_never_json_transient(monkeypatch):
    _session_ok()
    _Client.mode = "prose"
    monkeypatch.setattr(config_mod.settings, "OPENCODE_TIMEOUT_S", 9)
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_disabled_and_unconfigured(monkeypatch):
    _session_ok()
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", False)
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert "disabled" in (ei.value.message or "")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_BASE_URL", "")
    with pytest.raises(AppError) as ei2:
        run(gen.opencode_structured("x", {}))
    assert "unconfigured" in (ei2.value.message or "")


def test_chain_falls_back_through_groq_to_gemini(monkeypatch):
    import httpx as httpx_mod
    _Client.next_post = httpx_mod.ConnectError("refused")  # server down
    calls: list = []

    async def _groq(prompt, schema):
        calls.append("groq")
        raise AppError("E_PROVIDER_TRANSIENT", "groq down", 502)

    async def _gemini(prompt, schema):
        calls.append("gemini")
        return {"data": {"ok": True}, "provider": "gemini", "model": "m"}

    monkeypatch.setattr(gen, "groq_structured", _groq)
    monkeypatch.setattr(gen, "gemini_structured", _gemini)
    out = run(gen.structured_generate("Do it.", {"type": "object"}))
    assert out["provider"] == "gemini" and calls == ["groq", "gemini"]


def test_chain_fatal_stops_cascade(monkeypatch):
    _Client.next_post = _Resp(401, {})

    async def _groq(prompt, schema):
        raise AssertionError("must not reach groq on opencode fatal")

    monkeypatch.setattr(gen, "groq_structured", _groq)
    with pytest.raises(AppError) as ei:
        run(gen.structured_generate("Do it.", {}))
    assert ei.value.code == "E_PROVIDER_FATAL"


def test_groq_retries_transient_then_succeeds(monkeypatch):
    """Free-tier 429s clear in seconds: groq retries transient failures twice
    before giving up (previously one attempt turned a blip into a dead page)."""
    from types import SimpleNamespace

    monkeypatch.setattr(config_mod.settings, "GROQ_API_KEY", "test-key")
    calls = {"n": 0}

    def _create(**k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 rate limit exceeded, try again")
        msg = SimpleNamespace(content='{"records": [], "coverage": "none"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _FakeGroq:
        def __init__(self, *a, **k):
            pass

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **k):
            return _create(**k)

    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=_FakeGroq))
    out = run(gen.groq_structured("Do it.", {"type": "object"}))
    assert out["provider"] == "groq" and calls["n"] == 3


def test_groq_fatal_raises_at_once(monkeypatch):
    """Auth/400 failures never retry — the request is wrong, not unlucky."""
    from types import SimpleNamespace

    monkeypatch.setattr(config_mod.settings, "GROQ_API_KEY", "test-key")
    calls = {"n": 0}

    class _FakeGroq:
        def __init__(self, *a, **k):
            pass

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **k):
            calls["n"] += 1
            raise RuntimeError("401 invalid api key")

    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=_FakeGroq))
    with pytest.raises(AppError) as ei:
        run(gen.groq_structured("Do it.", {"type": "object"}))
    assert ei.value.code == "E_PROVIDER_FATAL" and calls["n"] == 1
