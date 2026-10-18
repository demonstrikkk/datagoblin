"""SSE event bus — per-run lock-guarded ring buffer (cap 2000, drop-oldest + counter).

Memory is O(1) per run regardless of run length. When the ring overflows the
oldest events fall off the front and `_dropped` counts what was lost.

**The count is not surfaced to anyone.** This docstring used to say it was, and
said the UI showed "N earlier events truncated". It does not: the accessors that
would have done it had zero callers in `app/` and in `tests/`, which is the same
reason `EventBus.format_sse` was deleted here — AUDIT.md had already flagged it
as a zero-caller deletion candidate and it stayed.

So today a client that was away long enough to overflow a run's ring resumes
against a silently truncated history. Two things are missing and neither is
guessable from this file: an accessor for `_dropped`, and an SSE `id:` frame so
a reconnecting client can ask for a position at all. Both belong with the replay
work; recording the count without reading it is deliberate, so the gap is
visible rather than inferred from a comment that promised otherwise.

Every emitted event is also written to `run_events`, which is not truncated --
the loss here is to the live stream only.
"""
import asyncio
from collections import deque

CAP = 2000


class EventBus:
    def __init__(self) -> None:
        self._events: dict[str, deque] = {}
        self._dropped: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._done: dict[str, bool] = {}

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    async def emit(self, run_id: str, event: dict) -> None:
        async with self._lock(run_id):
            buf = self._events.setdefault(run_id, deque(maxlen=CAP))
            if len(buf) == CAP:
                self._dropped[run_id] = self._dropped.get(run_id, 0) + 1
            buf.append(event)
            if event.get("type") in ("run.completed", "run.failed", "run.cancelled"):
                self._done[run_id] = True

    async def snapshot(self, run_id: str, after: int = 0) -> tuple[list[dict], int, bool]:
        async with self._lock(run_id):
            buf = list(self._events.get(run_id, []))
            return buf[after:], len(buf), self._done.get(run_id, False)

    def evict(self, run_id: str) -> None:
        """Drop a run's buffer. Safe only for terminal runs (they emit no more)."""
        self._events.pop(run_id, None)
        self._dropped.pop(run_id, None)
        self._locks.pop(run_id, None)
        self._done.pop(run_id, None)


bus = EventBus()
