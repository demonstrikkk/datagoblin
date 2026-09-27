"""Throttled is not the same as absent.

A busy judge answered HTTP 429 fourteen times in one live run, and every field
came back as `judgment_unavailable` - indistinguishable from "we had no judge to
ask". Those call for different responses: one is worth backing off and retrying,
the other is a fact about the deployment. Lumping them together hid a real
operational condition behind a plausible-looking status.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.providers.llm import zen  # noqa: E402
from app.services import provenance as provenance_svc  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


PAGE = "Acme Corp was founded in 2011 and employs roughly 240 people."
QUOTE = "employs roughly 240 people"
# A value NOT contained in the quote, so the judge is genuinely consulted.
NEEDS_JUDGE = ("a mid-sized European firm", QUOTE)


# --- the transport ---------------------------------------------------------

def _resp(status, headers=None, payload=None):
    class R:
        def __init__(self):
            self.status_code = status
            self.headers = headers or {}
            self._p = payload or {}
            self.text = ""

        def json(self):
            return self._p
    return R()


def test_429_raises_a_distinct_signal(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ZEN_JEV_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "JEV_MAX_RETRIES", 0)
    monkeypatch.setattr(zen, "_key", lambda: "k")
    monkeypatch.setattr(zen, "_base", lambda: "https://example.test")

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _resp(429)

    monkeypatch.setattr(zen.httpx, "AsyncClient", lambda **k: C())
    with pytest.raises(zen.JevRateLimited):
        run(zen.systemone("s", {"q": {}}))


def test_429_is_retried_with_backoff_before_giving_up(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ZEN_JEV_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "JEV_MAX_RETRIES", 2)
    monkeypatch.setattr(zen, "_key", lambda: "k")
    monkeypatch.setattr(zen, "_base", lambda: "https://example.test")
    slept = []
    calls = {"n": 0}

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(zen.asyncio, "sleep", _sleep)

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            return _resp(429)

    monkeypatch.setattr(zen.httpx, "AsyncClient", lambda **k: C())
    with pytest.raises(zen.JevRateLimited):
        run(zen.systemone("s", {"q": {}}))
    assert calls["n"] == 3, "expected 1 attempt + 2 retries"
    assert len(slept) == 2 and slept[1] > slept[0], "backoff must grow"


def test_retry_after_header_is_honoured_and_capped(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ZEN_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ZEN_JEV_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "JEV_MAX_RETRIES", 1)
    monkeypatch.setattr(zen, "_key", lambda: "k")
    monkeypatch.setattr(zen, "_base", lambda: "https://example.test")
    slept = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(zen.asyncio, "sleep", _sleep)

    class C:
        async def __aexit__(self, *a):
            return False

        async def __aenter__(self):
            return self

        async def post(self, *a, **k):
            return _resp(429, headers={"Retry-After": "7"})

    monkeypatch.setattr(zen.httpx, "AsyncClient", lambda **k: C())
    with pytest.raises(zen.JevRateLimited):
        run(zen.systemone("s", {"q": {}}))
    assert slept == [7.0]
    assert zen._retry_after_s(_resp(429, headers={"Retry-After": "9999"})) == 30.0, \
        "a hostile Retry-After must not stall the run"
    assert zen._retry_after_s(_resp(429)) is None
    assert zen._retry_after_s(_resp(429, headers={"Retry-After": "soon"})) is None


def test_throttling_does_not_silently_spend_the_paid_judge(monkeypatch, real_judge):
    """A free-tier 429 must not turn into a paid OpenRouter call."""
    paid = {"n": 0}

    async def _paid_boom(*a, **k):
        paid["n"] += 1
        return {}

    monkeypatch.setattr(jev, "_key", lambda: "paid-key")
    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda **k: _paid_boom())
    monkeypatch.setattr(config_mod.settings, "ZEN_JEV_ENABLED", True)

    async def _throttled(state, questions, **kw):
        raise zen.JevRateLimited("429")

    monkeypatch.setattr(zen, "systemone", _throttled)
    out = run(jev.evidence_verification(NEEDS_JUDGE[0], NEEDS_JUDGE[1], PAGE))
    assert out["judgment"] == "RATE_LIMITED"
    assert paid["n"] == 0, "a throttled free judge must not bill the paid one"


# --- the verdict and the stored status --------------------------------------

def test_verdict_reports_rate_limited_not_judgment_unavailable(monkeypatch, real_judge):
    async def _throttled(state, questions, **kw):
        raise zen.JevRateLimited("429 after 3 attempts")

    monkeypatch.setattr(zen, "systemone", _throttled)
    out = run(jev.evidence_verification(*NEEDS_JUDGE, PAGE))
    assert out["judgment"] == "RATE_LIMITED"
    assert out["provider"] == "throttled"


def test_stored_status_is_rate_limited(monkeypatch, real_judge):
    async def _throttled(state, questions, **kw):
        raise zen.JevRateLimited("429")

    monkeypatch.setattr(zen, "systemone", _throttled)
    monkeypatch.setattr(validator_svc, "JUDGE_BUDGET", validator_svc.JudgeBudget(0))
    v, st = run(validator_svc.verify_field(
        NEEDS_JUDGE[0], NEEDS_JUDGE[1], PAGE, False, "string"))
    assert st == "rate_limited", "throttling must be its own stored status"
    assert v == NEEDS_JUDGE[0], "the value is kept; only the ruling is missing"


def test_absence_is_still_judgment_unavailable(no_judge):
    """The two must not collapse into each other."""
    v, st = run(validator_svc.verify_field(
        NEEDS_JUDGE[0], NEEDS_JUDGE[1], PAGE, False, "string"))
    assert st == "judgment_unavailable"


def test_rate_limited_is_counted_separately():
    rows = [{"fields": {"a": {"verification_status": "verified"},
                        "b": {"verification_status": "rate_limited"},
                        "c": {"verification_status": "judgment_unavailable"}}}]
    s = provenance_svc.summarize(rows)
    assert s["fields_rate_limited"] == 1
    assert s["fields_judgment_unavailable"] == 1
    assert s["fields_verified"] == 1
    assert s["fields_verified"] + s["fields_rate_limited"] + \
        s["fields_judgment_unavailable"] == 3


def test_rate_limited_is_a_valid_schema_status():
    from app.schemas.plan import ProvenanceField
    f = ProvenanceField.model_validate(
        {"value": "x", "verification_status": "rate_limited",
         "source": {"url": "u", "quote": "q", "retrieved_at": "t"}})
    assert f.verification_status == "rate_limited"


# --- health -----------------------------------------------------------------

def test_health_reports_jev_counters():
    from app import main as main_mod
    import inspect
    src = inspect.getsource(main_mod.health)
    assert "JEV_HEALTH" in src, "throttling must be observable via /api/health"
