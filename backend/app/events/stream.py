"""SSE event bus — per-run lock-guarded ring buffer (cap 2000, drop-oldest + counter).

Memory is O(1) per run regardless of run length. Dropped count is surfaced so the
UI can show "… N earlier events truncated" instead of silently missing history.
"""
import asyncio
import json
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

    def dropped(self, run_id: str) -> int:
        return self._dropped.get(run_id, 0)

    def evict(self, run_id: str) -> None:
        """Drop a run's buffer. Safe only for terminal runs (they emit no more)."""
        self._events.pop(run_id, None)
        self._dropped.pop(run_id, None)
        self._locks.pop(run_id, None)
        self._done.pop(run_id, None)

    @staticmethod
    def format_sse(event: dict) -> str:
        return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"


bus = EventBus()
