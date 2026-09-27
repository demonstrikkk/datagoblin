"""OpenCode-primary LLM chain tests (v1 message protocol). Hermetic: no network.

The fake client speaks the protocol verified against opencode 1.18.32:
    POST /session                      -> {"id": ...}
    POST /session/{sid}/message        -> {"info": {...}, "parts": [...]}
    DELETE /session/{sid}
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.providers.llm import generate as gen  # noqa: E402
from app.providers.llm import opencode as oc  # noqa: E402


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
    next_post: object = None
    next_message: object = None
    #: Set to a model id to make the fake report it served a DIFFERENT model
    #: than the one pinned, which is the silent-fallback case.
    served_override: str = ""
    #: Delay the /message reply so concurrent calls genuinely overlap in time.
    message_delay: float = 0.0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _message(self, body=None):
        if isinstance(_Client.next_message, _Resp):
            return _Client.next_message
        text = ("Sorry, here is prose without any JSON object in it at all"
                if _Client.mode == "prose" else _Client.answer)
        # A real server echoes the model it was pinned to. Faking a fixed id
        # here would have hidden the silent-fallback bug, so the fake honours
        # the request exactly as the live server does.
        requested = ""
        if isinstance(body, dict):
            requested = ((body.get("model") or {}).get("modelID") or "")
        served = _Client.served_override or requested or "test-model"
        return _Resp(200, {"info": {"modelID": served,
                                    "providerID": "opencode", "cost": 0,
                                    "tokens": {"input": 10, "output": 5}},
                            "parts": [{"type": "text", "text": text,
                                       "synthetic": False}]})

    async def post(self, url, json=None, headers=None):
        _Client.seen.setdefault("posts", []).append({"url": url, "json": json,
                                                     "headers": headers})
        if isinstance(_Client.next_post, Exception):
            raise _Client.next_post
        if url.endswith("/message"):
            if _Client.message_delay:
                import asyncio as _a
                await _a.sleep(_Client.message_delay)
            return self._message(json)
        if _Client.next_post is not None and isinstance(_Client.next_post, _Resp):
            return _Client.next_post
        # Unique ids per session, as a real server issues, so the pool's
        # exclusive-checkout property is actually exercised.
        _Client.seen["n_sessions"] = _Client.seen.get("n_sessions", 0) + 1
        return _Resp(200, {"id": f"ses_{_Client.seen['n_sessions']}"})

    async def get(self, url, params=None, headers=None):
        _Client.seen["get"] = {"url": url, "params": params}
        return _Resp(200, {})

    async def delete(self, url, headers=None):
        _Client.seen.setdefault("deletes", []).append({"url": url,
                                                       "headers": headers})
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _opencode_cfg(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_BASE_URL",
                        "http://127.0.0.1:4096")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_PASSWORD", "pw")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_MODEL", "test-model")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_PROVIDER_ID", "opencode")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_TIMEOUT_S", 30)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    gen._reset_session_cache()
    _Client.seen = {}
    _Client.mode = "ok"
    _Client.next_post = None
    _Client.next_message = None
    _Client.served_override = ""
    yield
    gen._reset_session_cache()


def test_opencode_success_pins_model_in_body():
    """The regression that mattered: the model must travel in the message body.
    The old /api/session/{id}/model pin 400'd and was swallowed, so every
    request was silently answered by the server default."""
    out = run(gen.opencode_structured("Extract.", {"type": "object"}))
    assert out["provider"] == "opencode" and out["model"] == "test-model"
    assert out["data"] == {"records": [], "coverage": "none"}
    posts = _Client.seen["posts"]
    assert posts[0]["url"] == "http://127.0.0.1:4096/session"
    assert posts[1]["url"] == "http://127.0.0.1:4096/session/ses_1/message"
    assert posts[1]["json"]["model"] == {"providerID": "opencode",
                                         "modelID": "test-model"}
    assert "JSON object only" in posts[1]["json"]["parts"][0]["text"]
    assert posts[1]["headers"]["Authorization"].startswith("Basic ")


def test_prefixed_model_spec_is_split(monkeypatch):
    """Existing .env files carry `opencode/<model>`, which is how opencode's own
    config addresses Zen models. The v1 wire format needs provider and model as
    separate fields, so the prefix must be split, not sent as the modelID."""
    monkeypatch.setattr(config_mod.settings, "OPENCODE_MODEL",
                        "opencode/muse-spark-1.3-contributor-free")
    run(gen.opencode_structured("x", {}))
    posted = [p for p in _Client.seen["posts"] if p["url"].endswith("/message")][0]
    assert posted["json"]["model"] == {
        "providerID": "opencode",
        "modelID": "muse-spark-1.3-contributor-free"}


def test_split_model_helper():
    assert oc._split_model("big-pickle") == ("big-pickle", None)
    assert oc._split_model("opencode/big-pickle") == ("big-pickle", "opencode")
    assert oc._split_model("  opencode/big-pickle  ") == ("big-pickle", "opencode")
    assert oc._split_model("") == ("", None)


def test_opencode_reports_server_reported_model():
    """We return the model the SERVER says answered, not the one we asked for —
    a silent default swap is visible in the result instead of hidden."""
    _Client.next_message = _Resp(200, {
        "info": {"modelID": "server-default", "providerID": "opencode", "cost": 0},
        "parts": [{"type": "text", "text": '{"ok":true}'}]})
    out = run(gen.opencode_structured("x", {}))
    assert out["model"] == "server-default"


def test_opencode_no_polling_needed():
    """v1 is synchronous: no GET message poll, so no 4s dead wait per call."""
    run(gen.opencode_structured("x", {}))
    assert "get" not in _Client.seen


def test_opencode_401_fatal():
    _Client.next_post = _Resp(401, {})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_FATAL"


def test_opencode_500_transient():
    _Client.next_message = _Resp(500, {"message": "boom"})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_404_is_retryable_not_a_version_mismatch():
    """Measured against a live server: POST /session/{id}/message returned 200
    for every other model in the same run, and a genuinely unknown model id
    returns 500 UnknownError - not 404. So 404 means 'this model was not served
    on this request'. It must be retryable, and the message must not assert a
    version mismatch, which sends people to debug the wrong layer."""
    _Client.next_message = _Resp(404, {"name": "NotFound"})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"
    assert "version mismatch" not in ei.value.message.lower()


def test_opencode_404_message_names_the_real_cause():
    err = oc._status_error(404, "{}")
    assert err.code == "E_PROVIDER_TRANSIENT"
    assert "serve this model" in err.message
    assert "version mismatch" not in err.message.lower()


def test_opencode_down_transient():
    import httpx as httpx_mod
    _Client.next_post = httpx_mod.ConnectError("refused")
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_prose_never_json_transient():
    _Client.mode = "prose"
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_model_error_field_is_transient():
    """A 200 can still carry a model-side failure in info.error."""
    _Client.next_message = _Resp(200, {"info": {"modelID": "m",
                                                 "error": {"name": "UnknownError"}},
                                        "parts": []})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_empty_text_transient():
    _Client.next_message = _Resp(200, {"info": {"modelID": "m"}, "parts": []})
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_opencode_synthetic_parts_ignored():
    """Server-generated scaffolding is not the answer."""
    _Client.next_message = _Resp(200, {
        "info": {"modelID": "m"},
        "parts": [{"type": "text", "text": "tool chatter", "synthetic": True},
                  {"type": "text", "text": '{"records": []}'}]})
    out = run(gen.opencode_structured("x", {}))
    assert out["data"] == {"records": []}


def test_opencode_disabled_and_unconfigured(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", False)
    with pytest.raises(AppError) as ei:
        run(gen.opencode_structured("x", {}))
    assert "disabled" in (ei.value.message or "")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "OPENCODE_BASE_URL", "")
    with pytest.raises(AppError) as ei2:
        run(gen.opencode_structured("x", {}))
    assert "unconfigured" in (ei2.value.message or "")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_BASE_URL",
                        "http://127.0.0.1:4096")
    monkeypatch.setattr(config_mod.settings, "OPENCODE_MODEL", "")
    with pytest.raises(AppError) as ei3:
        run(gen.opencode_structured("x", {}))
    assert "OPENCODE_MODEL" in (ei3.value.message or "")


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


def test_strict_mode_opencode_is_the_reader(monkeypatch):
    """OPENCODE_STRICT=true: opencode result is final, cloud fallbacks never
    fire; opencode failure raises instead of cascading."""
    monkeypatch.setattr(config_mod.settings, "OPENCODE_STRICT", True)

    async def _oc(prompt, schema):
        return {"data": {"records": []}, "provider": "opencode", "model": "m"}

    async def _groq(prompt, schema):
        raise AssertionError("strict mode must not reach groq")

    async def _gemini(prompt, schema):
        raise AssertionError("strict mode must not reach gemini")

    monkeypatch.setattr(gen, "opencode_structured", _oc)
    monkeypatch.setattr(gen, "groq_structured", _groq)
    monkeypatch.setattr(gen, "gemini_structured", _gemini)
    out = run(gen.structured_generate("Do it.", {"type": "object"}))
    assert out["provider"] == "opencode"

    async def _oc_down(prompt, schema):
        raise AppError("E_PROVIDER_TRANSIENT", "opencode down", 502)

    monkeypatch.setattr(gen, "opencode_structured", _oc_down)
    with pytest.raises(AppError) as ei:
        run(gen.structured_generate("Do it.", {"type": "object"}))
    assert ei.value.code == "E_PROVIDER_TRANSIENT"


def test_session_reused_back_to_back():
    """Consecutive calls share one session (no second session-create post) —
    fewer free-tier sessions burned per page of chunks."""
    out1 = run(gen.opencode_structured("First.", {"type": "object"}))
    out2 = run(gen.opencode_structured("Second.", {"type": "object"}))
    assert out1["provider"] == out2["provider"] == "opencode"
    urls = [p["url"] for p in _Client.seen["posts"]]
    assert urls == ["http://127.0.0.1:4096/session",
                    "http://127.0.0.1:4096/session/ses_1/message",
                    "http://127.0.0.1:4096/session/ses_1/message"]
    assert _Client.seen.get("deletes", []) == []  # success keeps the session warm


def test_session_dropped_on_failure():
    """A failed session is deleted and evicted — dead sessions never pile up
    or get reused."""
    _Client.next_message = _Resp(500, {"message": "boom"})
    with pytest.raises(AppError):
        run(gen.opencode_structured("x", {}))
    assert [d["url"] for d in _Client.seen.get("deletes", [])] == [
        "http://127.0.0.1:4096/session/ses_1"]
    assert [e["sid"] for e in oc._session_pool] == []


def test_successful_session_returns_to_pool():
    """Warm-start without sharing: a healthy session is recycled for the next
    call instead of being torn down each time."""
    run(gen.opencode_structured("x", {}))
    assert [e["sid"] for e in oc._session_pool] == ["ses_1"]
    run(gen.opencode_structured("x", {}))
    # Reused, not a second session opened.
    assert _Client.seen["n_sessions"] == 1


def test_concurrent_calls_never_share_one_session():
    """The measured corruption: four concurrent messages on ONE session were all
    served by that session's model and every caller got the same reply. Two run
    workers would then record each other's extractions. Sessions must therefore
    be checked out exclusively, so N concurrent calls need N distinct sids."""
    import asyncio as _a

    async def go():
        return await _a.gather(*(oc.chat("test-model", f"p{i}") for i in range(4)))

    _Client.message_delay = 0.05  # hold the reply open so the calls really overlap
    res = _a.run(go())
    assert len(res) == 4
    assert _Client.seen["n_sessions"] == 4, "concurrent calls shared a session"


def test_requested_model_overrides_configured_default():
    """Regression: `settings.OPENCODE_MODEL or model` pinned EVERY call to the
    configured model, so a fan-out asking four models got four answers from one
    model. The requested model must win; the setting is only a fallback."""
    monkey = _Client
    run(gen.opencode_structured("x", {}))
    body = monkey.seen["posts"][-1]["json"]
    assert body["model"]["modelID"] == "test-model"


def test_mislabel_is_reported_not_hidden():
    """If the server answers with a different model than requested, the result
    must name the model that actually answered and flag the mismatch, so one
    model answering four times cannot masquerade as 4/4 agreement."""
    _Client.served_override = "some-other-model"
    out = run(gen.opencode_structured("x", {}))
    assert out["model"] == "some-other-model"
    assert out["model_mismatch"] is True
    assert out["model_requested"] == "test-model"


def test_available_never_raises():
    out = run(oc.available())
    assert out["reachable"] is True
    assert out["base_url"] == "http://127.0.0.1:4096"
