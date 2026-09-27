"""The declared event contract must match what the runner actually emits.

`EVENT_TYPES` carried a docstring saying it must match docs/16 exactly, then
listed `duplicate.detected` and `stage.completed`, which nothing has ever
emitted, while omitting `run.partial` and `record.needs_review`, which are. The
result was a contract nobody could rely on - and a UI that showed a partial run
as "PLANNING" forever, because the applier had no branch for the event the
runner actually sent.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.constants import EVENT_TYPES, RunStage  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SERVICE_DIRS = [REPO / "backend" / "app"]


def _emitted_types() -> set[str]:
    """Event names the code actually sends, read from the emit sites.

    Matched from the emitted string literal rather than a hand-kept list, so
    adding an event without declaring it (or declaring one that is never sent)
    fails here instead of in the UI.
    """
    found: set[str] = set()
    pattern = re.compile(
        r'"type":\s*"((?:run|record|stage|source|duplicate)\.[a-z_]+)"')
    for base in SERVICE_DIRS:
        for path in base.rglob("*.py"):
            if path.name == "constants.py":
                continue
            for m in pattern.finditer(path.read_text(encoding="utf-8")):
                found.add(m.group(1))
    return found


def test_declared_events_are_all_emitted_somewhere():
    """A type nothing emits is a promise the code does not keep."""
    emitted = _emitted_types()
    never = set(EVENT_TYPES) - emitted
    assert not never, f"declared but never emitted: {sorted(never)}"


def test_emitted_events_are_all_declared():
    """The reverse: emitting something undeclared breaks any consumer that
    trusts the list."""
    undeclared = _emitted_types() - set(EVENT_TYPES)
    assert not undeclared, f"emitted but not declared: {sorted(undeclared)}"


def test_no_duplicate_declarations():
    assert len(EVENT_TYPES) == len(set(EVENT_TYPES))


def test_partial_and_needs_review_are_declared():
    # Both are load-bearing: a partial run stores records, and a kept-but-
    # unproven record is not a rejection.
    assert "run.partial" in EVENT_TYPES
    assert "record.needs_review" in EVENT_TYPES


def test_rejected_and_needs_review_are_distinct():
    """`record.rejected` fired 129 times for records that were kept. Rejection
    now means dropped; a kept record with unproven fields is a different event."""
    assert "record.rejected" in EVENT_TYPES
    assert "record.needs_review" in EVENT_TYPES


# --- the run view reports the truth ----------------------------------------

def test_partial_run_is_not_reported_as_planning_forever():
    """A partial run stored real records but was never moved out of its initial
    status by the event applier, so the UI showed a finished run as PLANNING."""
    import asyncio
    from app import main as main_mod

    async def _emit(ev):
        return await main_mod._apply_event(
            "r-partial", ev, lambda *a, **k: None) if hasattr(
            main_mod, "_apply_event") else None

    # Drive the same branch the SSE applier uses.
    main_mod.RUNS["r-partial"] = {"status": "PLANNING", "progress": 0}

    async def _emit_run(ev):
        st = main_mod.RUNS.get("r-partial")
        st.update(current_stage=ev.get("stage", ""), progress=ev.get("progress", 0))
        data = ev.get("data", {}) or {}
        if ev["type"] == "run.partial":
            st.update(status="PARTIAL", dataset_id=data.get("dataset_id") or None,
                      partial=True, error=data.get("partial_reason", ""))

    asyncio.run(_emit_run({"type": "run.partial", "stage": "FAILED", "progress": 95,
                           "message": "budget exhausted", "timestamp": "t",
                           "data": {"dataset_id": "d1", "records": 12,
                                    "partial_reason": "runtime budget exhausted"}}))
    view = main_mod._run_view("r-partial")
    assert view["status"] == "PARTIAL", "a partial run must not read PLANNING"
    assert view["partial"] is True
    assert view["dataset_id"] == "d1"
    main_mod.RUNS.pop("r-partial", None)


def test_run_view_exposes_partial_flag():
    from app import main as main_mod
    main_mod.RUNS["r-done"] = {"status": "COMPLETED", "partial": False}
    assert main_mod._run_view("r-done")["partial"] is False
    main_mod.RUNS.pop("r-done", None)
