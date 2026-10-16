"""Tests for the run-event batcher.

Batching an audit trail introduces failure modes that per-event writes cannot
have, so these are mostly about what must NOT be lost or reordered:

  - a terminal event must be written immediately, and after everything queued
    before it, or the trail claims an outcome before the work that caused it;
  - a failed batch must degrade to per-event writes rather than dropping the
    whole batch;
  - a crash mid-run must not strand queued events;
  - blocking repository calls must never run on the event loop.
"""
import asyncio
import functools
import threading

import pytest

from app.services import eventbatch


def sync(fn):
    """Run a coroutine test to completion.

    The repo has no pytest-asyncio and uses `asyncio.run` directly (see
    `test_agentic.py`, `test_ask_reads_as_read.py`), so this follows that
    rather than adding a plugin dependency for one file.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


def _recorder(fail_bulk: bool = False):
    """Records calls and the thread each was made on.

    The thread list is what proves repository work stays off the event loop,
    which is the property most easily lost when this code is refactored.
    """
    calls: list[tuple[str, list[dict]]] = []
    threads: list[str] = []

    def one(rid, ev):
        calls.append((rid, [ev]))
        threads.append(threading.current_thread().name)

    def many(rid, evs):
        if fail_bulk:
            raise RuntimeError("bulk insert rejected")
        calls.append((rid, list(evs)))
        threads.append(threading.current_thread().name)

    return one, many, calls, threads


async def _submit(batcher, types, prefix="record"):
    for t in types:
        await batcher.submit({"type": t, "stage": "EXTRACTING"})


# -- the properties that must hold ---------------------------------------------

@sync
async def test_a_terminal_event_is_written_before_anything_still_queued():
    """Otherwise the trail shows the run ending before the records it produced."""
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1")

    await _submit(b, ["record.extracted", "record.extracted"])
    await b.submit({"type": "run.completed"})

    flattened = [ev["type"] for _rid, evs in calls for ev in evs]
    assert flattened == ["record.extracted", "record.extracted", "run.completed"]
    # The terminal event must not be sitting in a queue waiting for a timer.
    assert b.stats()["queued"] == 0


@pytest.mark.parametrize("terminal", sorted(eventbatch.TERMINAL_TYPES))
@sync
async def test_every_terminal_type_bypasses_the_queue(terminal):
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1")
    await b.submit({"type": terminal})
    assert b.stats()["queued"] == 0
    assert calls[-1][1][0]["type"] == terminal


@sync
async def test_a_full_batch_is_written_without_waiting_for_the_timer():
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=5, max_age_s=30)

    await _submit(b, ["record.extracted"] * 5)

    assert len(calls) == 1, "a full batch waited on the age timer"
    assert len(calls[0][1]) == 5


@sync
async def test_a_partial_batch_is_written_once_the_age_window_passes():
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=100, max_age_s=0.05)

    await b.submit({"type": "record.extracted"})
    await asyncio.sleep(0.2)

    assert len(calls) == 1, "the age window did not flush the partial batch"


@sync
async def test_a_failed_batch_degrades_to_per_event_rather_than_being_dropped():
    """One bad row must not cost the trail every other row in the batch."""
    one, many, calls, _ = _recorder(fail_bulk=True)
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=2, max_age_s=30)

    await _submit(b, ["record.extracted", "record.verified"])

    assert b.stats()["written"] == 2
    assert len(calls) == 2, "the batch was not retried one event at a time"


@sync
async def test_close_flushes_what_is_still_queued():
    """The crash and cancel paths rely on this."""
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=100, max_age_s=30)

    await _submit(b, ["record.extracted"] * 3)
    await b.aclose()

    assert b.stats()["queued"] == 0
    assert len(calls[-1][1]) == 3


@sync
async def test_flush_is_idempotent_and_does_not_duplicate():
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=100, max_age_s=30)
    await _submit(b, ["record.extracted"] * 2)

    await b.flush()
    await b.flush()
    await b.aclose()

    total = sum(len(evs) for _rid, evs in calls)
    assert total == 2, f"wrote {total} events for 2 submitted"


@sync
async def test_a_missing_bulk_path_still_persists_everything():
    """LocalRepo and any future adapter may not offer append_events."""
    one, _many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, None, "r1", max_batch=3, max_age_s=30)

    await _submit(b, ["record.extracted"] * 3)
    await b.aclose()

    assert b.stats()["written"] == 3


@sync
async def test_repository_calls_run_off_the_event_loop():
    """The reason the original handler used to_thread; easy to lose here."""
    one, many, _calls, threads = _recorder()
    loop_thread = threading.current_thread().name
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=2, max_age_s=30)

    await _submit(b, ["record.extracted"] * 2)
    await b.aclose()

    assert threads, "nothing was written"
    assert all(name != loop_thread for name in threads), \
        "a blocking repository call ran on the event loop"


@sync
async def test_submission_order_is_preserved_across_a_bulk_write():
    one, many, calls, _ = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=4, max_age_s=30)

    kinds = ["record.extracted", "record.verified", "record.needs_review",
             "record.rejected"]
    await _submit(b, kinds)
    await b.aclose()

    assert [ev["type"] for _r, evs in calls for ev in evs] == kinds


@sync
async def test_concurrent_submitters_do_not_lose_or_interleave_events():
    one, many, _calls, _t = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1", max_batch=7, max_age_s=30)

    async def worker(n):
        for i in range(10):
            await b.submit({"type": "record.extracted", "data": {"n": n, "i": i}})

    await asyncio.gather(*(worker(n) for n in range(6)))
    await b.aclose()

    assert b.stats()["written"] == 60, f"lost events: {b.stats()}"


@sync
async def test_stats_never_expose_event_payloads():
    one, many, _calls, _t = _recorder()
    b = eventbatch.EventBatcher(one, many, "r1")
    await b.submit({"type": "record.extracted", "message": "secret detail"})
    assert "secret detail" not in repr(b.stats())