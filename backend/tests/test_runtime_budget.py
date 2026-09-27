"""A run that runs out of time must keep what it earned, and say so.

The old timeout path emitted `Runtime budget exceeded (600s); partial kept` while
`store()` was never reached - so every extracted record was discarded and the
message described the opposite of what happened. It also wrapped the whole
pipeline in one `asyncio.wait_for`, so the budget was invisible to every stage:
a slow DISCOVERING consumed the whole allowance and FETCH/EXTRACT/VALIDATE never
ran at all.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config as config_mod  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.services import runner as runner_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


PLAN = {"goal": "find acme", "entity": "company", "requested_count": 3,
        "max_results": 3, "search_queries": ["acme"],
        "fields": [{"name": "company", "type": "string",
                    "description": "Name", "required": True}],
        "seed_urls": [], "allowed_sources": [], "max_pages": 3,
        "traversal": {"max_pages_per_domain": 3}, "validation_rules": [],
        "dedupe_keys": ["company"]}


def _ctx(pages=(), sleep_s=0.0, store=None):
    stored: dict = {}

    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        if sleep_s:
            await asyncio.sleep(sleep_s)
        return {"url": url, "title": f"Page {url[-1]}",
                "html": f"<html><body><p>Acme AI is a company. {url}</p></body></html>",
                "method": "http"}

    async def _llm(prompt, schema):
        return {"data": {"records": [
            {"fields": {"company": "Acme AI"},
             "evidence": [{"field": "company", "quote": "Acme AI is a company",
                           "source_url": prompt[:0] or "https://x.example/1"}]}],
            "coverage": "full"}, "provider": "test"}

    async def _store(rid, pl, rows, counts):
        stored.update(rows=rows, counts=counts)
        return "d1"

    async def _persist(s):
        pass

    ctx = {"search": _search, "fetch": _fetch, "llm": _llm,
           "store": store or _store, "persist_source": _persist,
           "cancelled": lambda: False}
    return ctx, stored


def _seeds(n):
    return [{"url": f"https://x.example/{i}", "title": ""} for i in range(n)]


# --- the budget is now visible to stages ------------------------------------

def test_budget_reports_remaining_and_expiry():
    b = runner_svc._Budget(60)
    assert b.remaining() <= 60 and b.remaining() > 0
    assert b.expired() is False
    zero = runner_svc._Budget(0)
    assert zero.remaining() == float("inf"), "0 must mean unlimited, not expired"
    assert zero.expired() is False


def test_expired_budget_is_detected(monkeypatch):
    """Driven off a stubbed clock: a real sleep makes this flaky under load,
    which is exactly the kind of test that trains people to ignore red."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(runner_svc.time, "monotonic", lambda: clock["t"])
    b = runner_svc._Budget(60)
    assert b.expired() is False
    clock["t"] += 59.0
    assert b.expired() is False
    assert round(b.remaining()) == 1
    clock["t"] += 2.0
    assert b.expired() is True
    assert b.remaining() == 0.0
    assert round(b.elapsed()) == 61


class _ScriptedBudget:
    """Expires after `n` checks, so the cut-off point is exact rather than a
    race against the wall clock."""

    def __init__(self, expire_after: int) -> None:
        self.left = expire_after
        self.total = 600.0
        self.started = 0.0

    def remaining(self):
        return 0.0 if self.left <= 0 else 60.0

    def expired(self):
        self.left -= 1
        return self.left <= 0

    def elapsed(self):
        return 12.0


def test_stages_stop_when_the_budget_is_gone_and_keep_the_records(monkeypatch):
    """Extraction is the slow stage, so the budget is checked per page. What was
    already extracted is validated, stored and reported as partial.

    Driven with a scripted budget so the cut-off is deterministic instead of a
    race: expire on the third check, which lands inside the per-page loop.
    """
    monkeypatch.setattr(config_mod.settings, "EXTRACT_PAGE_SPACING_S", 0)
    ctx, stored = _ctx()
    urls = _seeds(6)
    monkeypatch.setattr(runner_svc.discovery_svc, "discover",
                        lambda *a, **k: asyncio.sleep(0, result=urls))

    events: list = []

    async def _emit(ev):
        events.append(ev)

    budget = _ScriptedBudget(expire_after=3)
    progress: dict = {}
    res = run(runner_svc._body("r1", PLAN, ctx, _emit, budget, progress))

    assert res["status"] == "PARTIAL"
    assert res["partial"] is True
    assert stored.get("rows"), "extracted records must be kept, not discarded"
    assert stored["counts"]["partial"] is True
    assert "runtime budget" in stored["counts"]["partial_reason"]
    assert stored["counts"]["records"] == len(stored["rows"])
    # The event must report the same count that was actually stored.
    done = next(e for e in events if e["type"] == "run.partial")
    assert done["data"]["records"] == len(stored["rows"])
    assert done["data"]["dataset_id"] == "d1"


def test_budget_expiring_after_fetch_keeps_nothing_and_says_so(monkeypatch):
    """Bailing out before extraction leaves nothing to keep, and the run must
    say that rather than implying a partial result."""
    monkeypatch.setattr(config_mod.settings, "EXTRACT_PAGE_SPACING_S", 0)
    ctx, stored = _ctx()
    monkeypatch.setattr(runner_svc.discovery_svc, "discover",
                        lambda *a, **k: asyncio.sleep(0, result=_seeds(3)))
    events: list = []

    async def _emit(ev):
        events.append(ev)

    res = run(runner_svc._body("r1", PLAN, ctx, _emit,
                               _ScriptedBudget(expire_after=1), {}))
    assert res["status"] == "FAILED"
    assert res["records"] == 0
    assert not stored.get("rows")
    msg = next(e for e in events if e["type"] == "run.partial")["message"]
    assert "nothing was kept" in msg


def test_hard_timeout_keeps_already_validated_records(monkeypatch):
    """Even the last-resort timeout path stores what completed, rather than
    reporting 'partial kept' and keeping nothing."""
    stored: dict = {}
    progress_ready = asyncio.Event

    async def _hang(url, method):
        await asyncio.sleep(30)  # a stage that ignores its own bound

    async def _store(rid, pl, rows, counts):
        stored.update(rows=rows, counts=counts)
        return "d1"

    ctx, _ = _ctx(store=_store)
    ctx["fetch"] = _hang

    async def _discover(run_id, plan, search_fn, llm, emit):
        return _seeds(2)

    monkeypatch.setattr(runner_svc.discovery_svc, "discover", _discover)
    monkeypatch.setattr(config_mod.settings, "RUN_MAX_RUNTIME_S", 0.05)

    events: list = []

    async def _emit(ev):
        events.append(ev)

    res = run(runner_svc.execute_run("r1", PLAN, ctx, _emit))
    # Nothing was extracted before the hang, so there is honestly nothing to
    # keep - and the message must say that rather than claim a partial.
    assert res["records"] == 0
    assert not stored.get("rows"), "must not invent records"
    msg = next(e for e in events if e["type"] in ("run.partial", "run.failed"))["message"]
    assert "nothing was kept" in msg or "No records" in msg, msg


def test_no_partial_claim_when_nothing_survived(monkeypatch):
    """The specific lie being fixed: 'partial kept' with an empty dataset."""
    monkeypatch.setattr(config_mod.settings, "RUN_MAX_RUNTIME_S", 0.001)
    monkeypatch.setattr(config_mod.settings, "EXTRACT_PAGE_SPACING_S", 0)
    ctx, stored = _ctx()
    monkeypatch.setattr(runner_svc.discovery_svc, "discover",
                        lambda *a, **k: asyncio.sleep(0, result=_seeds(3)))
    events: list = []

    async def _emit(ev):
        events.append(ev)

    res = run(runner_svc.execute_run("r1", PLAN, ctx, _emit))
    if res["records"] == 0:
        assert not stored.get("rows")
        msgs = [e["message"] for e in events
                if e["type"] in ("run.partial", "run.failed")]
        assert any("nothing was kept" in m or "No records" in m for m in msgs), msgs


# --- a clean run is not partial --------------------------------------------

def test_full_run_is_not_flagged_partial(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "RUN_MAX_RUNTIME_S", 600)
    monkeypatch.setattr(config_mod.settings, "EXTRACT_PAGE_SPACING_S", 0)
    ctx, stored = _ctx()
    monkeypatch.setattr(runner_svc.discovery_svc, "discover",
                        lambda *a, **k: asyncio.sleep(0, result=_seeds(1)))

    async def _emit(ev):
        pass

    res = run(runner_svc.execute_run("r1", PLAN, ctx, _emit))
    assert res["status"] == "COMPLETED"
    assert res.get("partial") is not True
    assert stored["counts"]["partial"] is False
    assert stored["rows"], "a clean run must still produce records"


# --- the old message is gone -----------------------------------------------

def test_timeout_message_no_longer_claims_partial_kept(monkeypatch):
    src = Path(runner_svc.__file__).read_text(encoding="utf-8")
    assert "; partial kept" not in src, (
        "the message claimed records were kept while store() was never reached")
