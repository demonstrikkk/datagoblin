"""Free-model registry, transport dispatch and fan-out. Hermetic: no network.

These lock in the measured facts the feature rests on: most free models are
gated off direct REST, `ask()` picks the legal transport per model, and one
failing model never sinks a fan-out.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.core.errors import AppError, provider_fatal, provider_transient  # noqa: E402
from app.providers.llm import opencode as oc  # noqa: E402
from app.providers.llm import zen  # noqa: E402
from app.services import fanout  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_seconds):
    """Retry backoff collapsed so the suite stays fast."""


# --- registry ------------------------------------------------------------

def test_registry_has_no_duplicates_and_known_models():
    ids = [m["id"] for m in zen.free_models()]
    assert len(ids) == len(set(ids))
    for expected in ("space-bunny-free", "big-pickle", "muse-spark-1.3-contributor-free",
                     "muse-spark-1.2-contributor-free", "mimo-v2.5-free",
                     "mimo-v2.6-flash-free", "ling-3.0-flash-fin-free",
                     "nemotron-3-ultra-free", "nemotron-3.5-lightning-free",
                     "longcat-2.5-preview-free", "jev-1.13-free"):
        assert expected in ids, expected


def test_registry_observations_are_advisory():
    """Free-tier availability rotates between runs, so a registry snapshot must
    not be recorded as a fixed verdict."""
    for m in zen.free_models():
        assert m["observed"], m["id"]
        assert not m["observed"].startswith("upstream")
        assert m["note"] is not None


def test_observed_on_is_dated():
    assert zen.OBSERVED_ON.count("-") == 2


def test_every_model_declares_a_known_transport():
    assert {m["transport"] for m in zen.free_models()} <= {"direct", "opencode",
                                                           "systemone"}


def test_only_space_bunny_is_direct_text():
    """Measured: every free text model except space-bunny-free returns 403
    FreeTierError on direct REST."""
    direct_text = [m["id"] for m in zen.free_models()
                   if m["transport"] == "direct" and m.get("kind") != "decision"]
    assert direct_text == ["space-bunny-free"]


def test_decision_model_is_not_a_text_model():
    assert "jev-1.13-free" not in zen.text_models()
    assert zen.model_info("jev-1.13-free")["kind"] == "decision"


def test_configured_models_defaults_to_all_text_models(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_FANOUT_MODELS", "")
    assert zen.configured_models() == zen.text_models()


def test_configured_models_override_parsed(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_FANOUT_MODELS",
                        "big-pickle, space-bunny-free ,")
    assert zen.configured_models() == ["big-pickle", "space-bunny-free"]


def test_is_free_rejects_unknown():
    assert zen.is_free("big-pickle") and not zen.is_free("gpt-4o")


# --- transport dispatch --------------------------------------------------

def test_ask_routes_gated_model_to_opencode(monkeypatch):
    seen = {}

    async def _fake_chat(model, prompt, **kw):
        seen["model"] = model
        return {"text": "hi", "model": model, "provider": "opencode",
                "latency_ms": 5, "cost": 0, "tokens": {}}

    monkeypatch.setattr(oc, "chat", _fake_chat)
    out = run(zen.ask("big-pickle", "q"))
    assert seen["model"] == "big-pickle"
    assert out["transport"] == "opencode" and out["provider"] == "opencode"


def test_ask_uses_direct_for_ungated_model(monkeypatch):
    called = {"n": 0}

    async def _fake_chat(model, prompt, **kw):
        called["n"] += 1
        return {"text": "hi", "model": model, "provider": "zen", "latency_ms": 5,
                "usage": {}, "cost": None}

    monkeypatch.setattr(zen, "chat", _fake_chat)
    out = run(zen.ask("space-bunny-free", "q"))
    assert called["n"] == 1 and out["transport"] == "direct"


def test_direct_call_to_gated_model_names_the_fix(monkeypatch):
    """The 403 must be actionable, not a bare auth error."""
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "k")
    monkeypatch.setattr(config_mod.settings, "ZEN_ENABLED", True)
    with pytest.raises(AppError) as ei:
        run(zen.chat("big-pickle", "q"))
    assert ei.value.http == 424
    assert ei.value.details.get("transport") == "opencode"
    assert "gated" in ei.value.message


def test_chat_rejects_decision_model(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "k")
    with pytest.raises(AppError) as ei:
        run(zen.chat("jev-1.13-free", "q"))
    assert "decision model" in ei.value.message


def test_http_error_classifies_gate_before_auth(monkeypatch):
    """A gated 403 must not be reported as bad credentials — the key is fine."""
    err = zen._http_error("big-pickle", 403,
                          '{"error":{"type":"FreeTierError","message":"x"}}')
    assert err.details.get("transport") == "opencode"


def test_chat_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "k")
    monkeypatch.setattr(config_mod.settings, "ZEN_MAX_RETRIES", 2)
    monkeypatch.setattr(config_mod.settings, "ZEN_TIMEOUT_S", 5)
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self._payload = payload or {}
            self.text = text or str(self._payload)

        def json(self):
            return self._payload

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Resp(429, {}, "429 rate limit")
            return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _C())
    monkeypatch.setattr(zen.asyncio, "sleep", _no_sleep)
    out = run(zen.chat("space-bunny-free", "q"))
    assert out["text"] == "ok" and calls["n"] == 3


def test_chat_fatal_never_retries(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "k")
    calls = {"n": 0}

    class _Resp:
        status_code = 400
        text = "400 invalid request"

        def json(self):
            return {}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _C())
    with pytest.raises(AppError) as ei:
        run(zen.chat("space-bunny-free", "q"))
    assert ei.value.http == 424 and calls["n"] == 1


def test_chat_unconfigured_is_fatal(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "")
    with pytest.raises(AppError) as ei:
        run(zen.chat("space-bunny-free", "q"))
    assert "ZEN_API_KEY" in ei.value.message


def test_systemone_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_API_KEY", "")
    assert run(zen.systemone("state", {})) is None


def test_parse_json_lenient_variants():
    assert zen.parse_json_lenient('{"a":1}') == {"a": 1}
    assert zen.parse_json_lenient('```json\n{"a":1}\n```') == {"a": 1}
    assert zen.parse_json_lenient('sure! {"a":1} hope that helps') == {"a": 1}
    assert zen.parse_json_lenient("no json here") is None
    assert zen.parse_json_lenient("") is None


# --- fan-out -------------------------------------------------------------

def test_fanout_isolates_one_failing_model(monkeypatch):
    """Two models 500 upstream today; the other answers must still return."""
    async def _ask(model, prompt, **kw):
        if model in ("mimo-v2.5-free", "muse-spark-1.2-contributor-free"):
            raise AppError("E_PROVIDER_TRANSIENT", "server error (HTTP 500)")
        return {"text": '{"answer": 42}', "model": model, "provider": "opencode",
                "latency_ms": 3, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    out = run(fanout.ask_many("q", zen.text_models(), json_mode=True))
    rows = {r["model"]: r for r in out["results"]}
    assert len(out["results"]) == len(zen.text_models())
    assert rows["big-pickle"]["ok"] and rows["big-pickle"]["answer"] == {"answer": 42}
    assert not rows["mimo-v2.5-free"]["ok"]
    assert "500" in rows["mimo-v2.5-free"]["error"]
    assert out["consensus"]["answered"] == len(zen.text_models()) - 2


def test_fanout_consensus_unanimous(monkeypatch):
    async def _ask(model, prompt, **kw):
        return {"text": '{"answer": 42}', "model": model, "provider": "opencode",
                "latency_ms": 1, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    out = run(fanout.ask_many("q", zen.text_models(), json_mode=True))
    assert out["consensus"]["agreement"] == "unanimous"
    assert out["consensus"]["unanimous"] is True
    assert out["consensus"]["agreement_ratio"] == 1.0


def test_fanout_consensus_surfaces_disagreement(monkeypatch):
    async def _ask(model, prompt, **kw):
        val = 42 if model != "mimo-v2.5-free" else 7
        return {"text": '{"answer": %d}' % val, "model": model,
                "provider": "opencode", "latency_ms": 1, "cost": 0,
                "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    out = run(fanout.ask_many("q", zen.text_models(), json_mode=True))
    assert out["consensus"]["agreement"] == "majority"
    assert len(out["consensus"]["clusters"]) == 2
    assert "not evidence" in out["consensus"]["note"]


def test_fanout_prose_mode_keeps_raw_text(monkeypatch):
    """Without json_mode the answer is the text itself, not a parsed object."""
    async def _ask(model, prompt, **kw):
        return {"text": "42", "model": model, "provider": "opencode",
                "latency_ms": 1, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    out = run(fanout.ask_many("q", ["big-pickle"]))
    assert out["results"][0]["answer"] == "42"


def test_fanout_all_fail_returns_empty_consensus(monkeypatch):
    async def _ask(model, prompt, **kw):
        raise AppError("E_PROVIDER_TRANSIENT", "down")

    monkeypatch.setattr(zen, "ask", _ask)
    out = run(fanout.ask_many("q", zen.text_models()))
    assert out["consensus"]["answered"] == 0
    assert out["consensus"]["agreement"] is None
    assert all(not r["ok"] for r in out["results"])


def test_fanout_respects_concurrency(monkeypatch):
    live = {"now": 0, "peak": 0}

    async def _ask(model, prompt, **kw):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return {"text": "x", "model": model, "provider": "opencode",
                "latency_ms": 1, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    run(fanout.ask_many("q", zen.text_models(), concurrency=2))
    assert live["peak"] <= 2


def test_fanout_clamps_model_count(monkeypatch):
    asked: list = []

    async def _ask(model, prompt, **kw):
        asked.append(model)
        return {"text": "x", "model": model, "provider": "opencode",
                "latency_ms": 1, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    run(fanout.ask_many("q", [f"m{i}" for i in range(50)]))
    assert len(asked) == fanout.MAX_MODELS


def test_fanout_deduplicates_and_orders(monkeypatch):
    asked: list = []

    async def _ask(model, prompt, **kw):
        asked.append(model)
        return {"text": "x", "model": model, "provider": "opencode",
                "latency_ms": 1, "cost": 0, "tokens": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _ask)
    run(fanout.ask_many("q", ["big-pickle", "space-bunny-free", "big-pickle"]))
    assert asked == ["space-bunny-free", "big-pickle"]  # registry order, deduped


def test_fanout_no_models_selected():
    out = run(fanout.ask_many("q", []))
    assert out["results"] == [] and out["models"] == []


def test_catalogue_shape():
    cat = fanout.catalogue()
    assert cat["counts"]["text"] == len(zen.text_models())
    assert cat["counts"]["decision"] == 1
    assert "opencode" in cat["transports"] and "direct" in cat["transports"]
    assert "gated_note" in cat


def test_consensus_normalises_numeric_noise():
    """3.0 and 3.0000001 are agreement, not a conflict."""
    assert fanout._normalise(3.0) == fanout._normalise(3.0000001)
    assert fanout._normalise(True) == "true"
    assert fanout._normalise("A  B") == fanout._normalise("a b")


def test_consensus_ignores_json_formatting():
    """Regression: three models that all answered {"capital": "Paris"} were
    reported as a 2-1 'majority' purely because one omitted the space after the
    colon. Whitespace and key order are typography, not disagreement."""
    results = [
        {"model": "a", "ok": True, "answer": '{"capital":"Paris"}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"capital": "Paris"}', "text": ""},
        {"model": "c", "ok": True, "answer": '{\n  "capital": "Paris"\n}', "text": ""},
    ]
    c = fanout._consensus(results)
    assert c["unanimous"] is True, c
    assert c["agreement"] == "unanimous"
    assert c["agreement_ratio"] == 1.0
    assert len(c["clusters"]) == 1
    assert c["clusters"][0]["count"] == 3


def test_consensus_ignores_key_order():
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": {"x": 1, "y": 2}, "text": ""},
        {"model": "b", "ok": True, "answer": {"y": 2, "x": 1}, "text": ""},
    ])
    assert c["unanimous"] is True, c


def test_consensus_still_separates_real_disagreement():
    """The formatting fix must not merge genuinely different answers."""
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": '{"capital": "Paris"}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"capital": "Berlin"}', "text": ""},
    ])
    assert c["unanimous"] is False
    assert len(c["clusters"]) == 2
    assert c["agreement"] == "split"


def test_consensus_still_separates_different_numbers():
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": '{"n": 3}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"n": 4}', "text": ""},
    ])
    assert c["unanimous"] is False
    assert len(c["clusters"]) == 2


def test_consensus_tolerates_3dp_rounding_in_json():
    """3, 3.0 and 3.0000001 nested in a JSON object are one answer — the case
    json_mode actually produces, which the bare-number branch never covered."""
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": '{"n": 3}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"n": 3.0}', "text": ""},
        {"model": "c", "ok": True, "answer": '{"n": 3.0000001}', "text": ""},
    ])
    assert c["unanimous"] is True, c


def test_consensus_does_not_confuse_number_with_string():
    """Rounding must not merge the number 3 with the string "3"."""
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": '{"n": 3}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"n": "3"}', "text": ""},
    ])
    assert c["unanimous"] is False, c
    assert len(c["clusters"]) == 2


def test_consensus_does_not_confuse_bool_with_number():
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": '{"ok": true}', "text": ""},
        {"model": "b", "ok": True, "answer": '{"ok": 1}', "text": ""},
    ])
    assert c["unanimous"] is False, c


def test_consensus_prose_answers_still_cluster():
    """Non-JSON prose falls back to whitespace-collapsed text matching."""
    c = fanout._consensus([
        {"model": "a", "ok": True, "answer": "The  capital   is Paris", "text": ""},
        {"model": "b", "ok": True, "answer": "the capital is paris", "text": ""},
        {"model": "c", "ok": True, "answer": "Berlin", "text": ""},
    ])
    assert c["unanimous"] is False
    assert c["clusters"][0]["count"] == 2
    assert c["agreement"] == "majority"


async def _no_sleep(_seconds):
    return None


# --- upstream flakiness must not permanently cost coverage -----------------

def test_opencode_transport_retries_transient_500(monkeypatch):
    """Measured live: one model in a 4-model fan-out died on a bare HTTP 500 and
    was lost for good, which is what showed up as 65% coverage. A transient
    failure gets retried so a blip costs one attempt, not the model."""
    monkeypatch.setattr(config_mod.settings, "ZEN_MAX_RETRIES", 2)
    calls = {"n": 0}

    async def _flaky_chat(model, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise provider_transient("OpenCode server error (HTTP 500): boom")
        return {"text": "hi", "model": model, "provider": "opencode",
                "latency_ms": 5, "cost": 0, "tokens": {}}

    monkeypatch.setattr(oc, "chat", _flaky_chat)
    monkeypatch.setattr(zen.asyncio, "sleep", _no_sleep)
    out = run(zen.ask("big-pickle", "q"))
    assert calls["n"] == 2
    assert out["attempts"] == 2
    assert out["text"] == "hi"


def test_opencode_transport_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_MAX_RETRIES", 1)
    calls = {"n": 0}

    async def _always_500(model, prompt, **kw):
        calls["n"] += 1
        raise provider_transient("OpenCode server error (HTTP 500): boom")

    monkeypatch.setattr(oc, "chat", _always_500)
    monkeypatch.setattr(zen.asyncio, "sleep", _no_sleep)
    with pytest.raises(AppError):
        run(zen.ask("big-pickle", "q"))
    assert calls["n"] == 2, "retries must be bounded, not unbounded"


def test_opencode_transport_does_not_retry_fatal(monkeypatch):
    """Auth/refusal is a configuration fault; retrying it only burns budget."""
    monkeypatch.setattr(config_mod.settings, "ZEN_MAX_RETRIES", 3)
    calls = {"n": 0}

    async def _fatal(model, prompt, **kw):
        calls["n"] += 1
        raise provider_fatal("OpenCode auth failed (HTTP 401)")

    monkeypatch.setattr(oc, "chat", _fatal)
    with pytest.raises(AppError) as ei:
        run(zen.ask("big-pickle", "q"))
    assert ei.value.code == "E_PROVIDER_FATAL"
    assert calls["n"] == 1


def test_ask_passes_through_mislabel_provenance(monkeypatch):
    async def _fake_chat(model, prompt, **kw):
        return {"text": "hi", "model": "other-model", "provider": "opencode",
                "latency_ms": 5, "cost": 0, "tokens": {},
                "model_requested": model, "model_mismatch": True}

    monkeypatch.setattr(oc, "chat", _fake_chat)
    out = run(zen.ask("big-pickle", "q"))
    assert out["model"] == "other-model"
    assert out["model_mismatch"] is True


# --- provenance must not be fabricated -------------------------------------

def test_fanout_row_records_the_model_that_actually_answered(monkeypatch):
    async def _fake_ask(model, prompt, **kw):
        return {"text": "hi", "model": "actually-this-one", "provider": "opencode",
                "latency_ms": 5, "cost": 0, "usage": {}, "transport": "opencode"}

    monkeypatch.setattr(zen, "ask", _fake_ask)
    out = run(fanout.ask_many("q", ["big-pickle"], json_mode=True))
    row = next(r for r in out["results"] if r["model"] == "big-pickle")
    assert row["served_by"] == "actually-this-one"
    assert row["model_mismatch"] is True


def test_one_model_answering_many_times_is_not_independent_agreement():
    """The measured corruption: a mis-pinned transport served every request from
    one model, so four rows 'agreed' 4/4 while only one model was consulted.
    That must be reported as a single source, not as unanimous consensus."""
    c = fanout._consensus([
        {"model": "a", "ok": True, "served_by": "muse", "answer": '{"x":1}', "text": ""},
        {"model": "b", "ok": True, "served_by": "muse", "answer": '{"x":1}', "text": ""},
        {"model": "c", "ok": True, "served_by": "muse", "answer": '{"x":1}', "text": ""},
    ])
    assert c["agreement"] == "single-model"
    assert c["independent"] is False
    assert c["unanimous"] is False
    assert c["distinct_models"] == 1
    assert "not independent agreement" in c["note"]


def test_genuine_multi_model_agreement_is_still_independent():
    c = fanout._consensus([
        {"model": "a", "ok": True, "served_by": "a", "answer": '{"x":1}', "text": ""},
        {"model": "b", "ok": True, "served_by": "b", "answer": '{"x":1}', "text": ""},
    ])
    assert c["agreement"] == "unanimous"
    assert c["independent"] is True
    assert c["distinct_models"] == 2


def test_dead_opencode_server_is_not_retried(monkeypatch):
    """Measured: with the local server stopped, retrying every model turned an
    19s fan-out into 61s and still produced nothing. A refused connection is a
    down dependency, not a blip, so it must fail immediately with a fixable
    message instead of burning the budget."""
    monkeypatch.setattr(config_mod.settings, "ZEN_MAX_RETRIES", 3)
    calls = {"n": 0}

    async def _refused(model, prompt, **kw):
        calls["n"] += 1
        raise provider_transient("OpenCode unreachable: All connection attempts failed")

    monkeypatch.setattr(oc, "chat", _refused)
    with pytest.raises(AppError) as ei:
        run(zen.ask("big-pickle", "q"))
    assert calls["n"] == 1, "a down dependency must not be retried"
    assert "unreachable" in ei.value.message
