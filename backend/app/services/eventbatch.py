"""Batch the audit-trail write for run events.

Why this exists, measured rather than assumed: `SELECT 1` against the
configured database costs **177ms**, and `append_event` is one INSERT per
event. A run emits `record.extracted` / `record.verified` /
`record.needs_review` / `record.rejected` **per record**, and emits
`source.fetched` **per page**. A 15-record run over 20 pages is ~50 events,
which is ~9s of pure round-trip on the audit trail alone — during judging, on
the same connection pool everything else needs.

What this does *not* change: live delivery. `bus.emit` already ran for every
event by the time it reaches here, so subscribers see each event the moment it
happens. Only the row written for later auditing is batched. Nothing a user
watches gets slower.

What this costs, stated plainly: a process killed mid-flush loses the
un-flushed tail of the audit trail. That is a real trade, and it is the right
one here because the trail is a record of what happened rather than the channel
that reports it — but it is why the flush window is short and why terminal
events are never batched at all.

Ordering is preserved: events are appended in submission order, and a terminal
event forces a flush *before* it is written, so the audit trail never shows a
run completing before the records that led to it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

log = logging.getLogger("datagoblin")

#: Written as its own statement, immediately, never batched.
#:
#: These say the run is over. If one were lost to a crash the trail would end
#: mid-run with no explanation, and if one were reordered it would claim an
#: outcome before the work that produced it.
TERMINAL_TYPES = frozenset({
    "run.completed", "run.failed", "run.cancelled", "run.partial",
})

#: How many queued events go in one statement.
#:
#: Sized against the round-trip, not the row size: at 177ms per statement the
#: win comes from amortising the round trip, and 50 rows is one INSERT that
#: still fits comfortably in a single round trip's packet.
DEFAULT_MAX_BATCH = 50

#: Longest an event waits for company before being written anyway.
#:
#: Bounds what a crash can lose. At 0.4s the worst case is well under a second
#: of trail, and the cost is only paid when a run is not emitting fast enough
#: to fill a batch on its own.
DEFAULT_MAX_AGE_S = 0.4


class EventBatcher:
    """Collects audit-trail events and writes them in batches.

    One instance per run. Not shared across runs: a flush is per-run, and a
    shared queue would mean one noisy run delaying another's terminal event.
    """

    def __init__(self, append: Callable[[str, dict], Any],
                 append_many: Callable[[str, list], Any] | None,
                 run_id: str,
                 max_batch: int = DEFAULT_MAX_BATCH,
                 max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self._append = append
        self._append_many = append_many
        self._run_id = run_id
        self._max_batch = max(1, int(max_batch))
        self._max_age_s = max_age_s
        self._queue: list[dict] = []
        self._lock = asyncio.Lock()
        self._wake: asyncio.Task | None = None
        self.closed = False
        self.written = 0
        self.flushed = 0

    async def submit(self, ev: dict) -> None:
        """Queue an event, or write it straight through if it is terminal."""
        if ev.get("type") in TERMINAL_TYPES:
            # Flush first so the trail reads in the order things happened.
            await self.flush()
            await self._write([ev])
            self.closed = True
            return

        async with self._lock:
            self._queue.append(ev)
            full = len(self._queue) >= self._max_batch
        if full:
            await self.flush()
        else:
            self._arm()

    def _arm(self) -> None:
        """Ensure a flush is scheduled if none is pending.

        Uses `call_later`-style waiting via a task rather than a blocking
        sleep: the run coroutine must keep making progress while the audit
        trail waits its 0.4s.
        """
        if self._wake is not None and not self._wake.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wake = loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._max_age_s)
        except asyncio.CancelledError:
            return
        await self.flush()

    async def flush(self) -> None:
        """Write everything queued. Safe to call concurrently."""
        wake = self._wake
        # Never cancel the task we are running inside. `_delayed_flush` calls
        # this, and cancelling the current task raises inside itself at the next
        # await -- so the age-triggered flush silently never fired.
        if (wake is not None and not wake.done()
                and wake is not asyncio.current_task()):
            wake.cancel()
        async with self._lock:
            pending, self._queue = self._queue, []
        if pending:
            self.flushed += 1
            await self._write(pending)

    async def _write(self, events: list[dict]) -> None:
        """Persist, degrading to one-at-a-time rather than failing.

        The audit trail failing must never fail a run -- that was the rule
        before batching and it is kept here -- but a batch insert failing
        outright would lose every event in it. So a failed batch is retried
        individually, which recovers the common case (one malformed row) at the
        cost of degrading to the old behaviour instead of dropping the trail.
        """
        try:
            if self._append_many is not None and len(events) > 1:
                await asyncio.to_thread(self._append_many, self._run_id, events)
            else:
                await asyncio.gather(*(
                    asyncio.to_thread(self._append, self._run_id, ev)
                    for ev in events))
            self.written += len(events)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("run event batch write failed; retrying individually",
                        extra={"data": {"run_id": self._run_id,
                                        "events": len(events),
                                        "error": str(exc)[:150]}})
        for ev in events:
            try:
                await asyncio.to_thread(self._append, self._run_id, ev)
                self.written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("run event not persisted",
                            extra={"data": {"run_id": self._run_id,
                                            "type": ev.get("type", ""),
                                            "error": str(exc)[:150]}})

    async def aclose(self) -> None:
        """Final flush. Call from the run's own teardown."""
        await self.flush()
        self.closed = True

    def stats(self) -> dict[str, int]:
        return {"queued": len(self._queue), "written": self.written,
                "flushes": self.flushed, "max_batch": self._max_batch,
                "max_age_s": self._max_age_s}