"""`preflight()` must distinguish "no provider" from "not the primary one".

The check exists to stop a run that searches, crawls, extracts nothing and
reports success -- a live run fetched twelve pages, spent three minutes and
produced zero records because every extract call came back 401. Refusing is
right.

What it used to do instead: refuse every run whose local `opencode serve` was
not running, whether or not any cloud provider was configured. The comment
directly above the `return` said a cloud fallback "is only a *fallback* if a
cloud key is actually configured" -- and then never checked. So with
`GEMINI_API_KEY` and `GROQ_API_KEY` both set and working, and the plan compiler
having just used Gemini successfully in the same process, the run was still
refused with "All connection attempts failed at http://127.0.0.1:4096; start it
with `opencode serve`".

That made the pipeline unrunnable without a separate local server that nothing
documented as a requirement, and it was found by running a real investigation
rather than by reading the function.

The distinction, which is the one the comment already drew:

  * no cloud key configured  -> refuse. There is genuinely nothing to fall back
    to, and this is the failure the check exists to prevent.
  * a cloud key configured   -> proceed, loudly. Paying a different provider is
    worth saying out loud and is not worth refusing a working run for.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.providers.llm import opencode  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _state(monkeypatch, *, reachable=False, error="unreachable", enabled=True):
    async def _available():
        return {"reachable": reachable, "base_url": "http://127.0.0.1:4096",
                "enabled": enabled, "error": error}
    monkeypatch.setattr(opencode, "available", _available)


def _keys(monkeypatch, gemini="", groq=""):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", gemini)
    monkeypatch.setattr(settings, "GROQ_API_KEY", groq)
    monkeypatch.setattr(settings, "OPENCODE_STRICT", False)


def test_a_reachable_reader_passes(monkeypatch):
    _keys(monkeypatch, gemini="g")
    _state(monkeypatch, reachable=True, error="")
    assert run(opencode.preflight()) is None


def test_an_unreachable_reader_with_no_cloud_key_is_refused(monkeypatch):
    """The failure this function exists to prevent.

    Nothing to fall back to, so refusing is right: a run that would crawl twelve
    pages and extract nothing must not start.
    """
    _keys(monkeypatch, gemini="", groq="")
    _state(monkeypatch, reachable=False)
    problem = run(opencode.preflight())
    assert problem, "with no provider at all, the run must be refused"
    assert "4096" in problem or "unreachable" in problem.lower()


def test_an_unreachable_reader_with_a_cloud_key_is_allowed(monkeypatch):
    """The bug. This refused, with a working provider configured."""
    _keys(monkeypatch, gemini="a-real-key")
    _state(monkeypatch, reachable=False)
    assert run(opencode.preflight()) is None, (
        "a configured cloud provider can serve the run; refusing it here made "
        "the pipeline unrunnable without a separate local server")


def test_groq_alone_is_enough(monkeypatch):
    _keys(monkeypatch, groq="a-real-key")
    _state(monkeypatch, reachable=False)
    assert run(opencode.preflight()) is None


def test_a_disabled_reader_is_allowed_when_it_is_a_deliberate_choice(monkeypatch):
    """Unchanged behaviour: turning the primary off is not a fault."""
    _keys(monkeypatch, gemini="")
    _state(monkeypatch, reachable=False, error="disabled")
    assert run(opencode.preflight()) is None


def test_strict_mode_still_refuses_every_unusable_state(monkeypatch):
    """`OPENCODE_STRICT` means opencode is the only reader.

    The cloud allowance must not leak into it -- that is the entire point of the
    setting, and `test_discovery_model_down` pins the same property on the
    discovery path.
    """
    _keys(monkeypatch, gemini="a-real-key")
    _state(monkeypatch, reachable=False)
    monkeypatch.setattr(settings, "OPENCODE_STRICT", True)
    problem = run(opencode.preflight())
    assert problem, "strict mode has no fallback, so it must not allow the run"
    assert "OPENCODE_STRICT" in problem, (
        "the message has to say which setting is responsible")


def test_the_fallback_is_announced_rather_than_silent(monkeypatch, caplog):
    """A run that quietly starts paying a different provider is the thing to avoid.

    So the allowance is allowed, but never quiet: the comment promised "reported
    either way" and that half of the promise is what has to survive.
    """
    _keys(monkeypatch, gemini="a-real-key")
    _state(monkeypatch, reachable=False)
    with caplog.at_level("WARNING"):
        run(opencode.preflight())
    assert any("cloud provider" in r.message for r in caplog.records), (
        "the run was allowed onto a fallback and said nothing about it")


def test_the_comment_matches_the_code(monkeypatch):
    """The stated intent and the behaviour had drifted apart.

    Worth a test because that drift is invisible: the comment read correctly and
    described what the function should have done, and only running the thing
    showed it did something else.
    """
    import inspect

    src = inspect.getsource(opencode.preflight)
    assert "GEMINI_API_KEY" in src, (
        "the function promises to consider a configured cloud fallback and no "
        "longer does")