"""Phase-3 item pipeline: ordered per-record stages with lifecycle + bounds.

Contract (Scrapy item-pipeline spirit, asyncio-native):
- A stage is (name, async process(item) -> item). Stages may define optional
  async open() / close() for handle/connection lifecycle (opened in order,
  closed in reverse — appendleft semantics).
- process returns the item for the next stage, or raises DropItem(reason) to
  remove it with an accounted reason. Any OTHER exception propagates: a stage
  bug fails loud, never silently drops records.
- run() preserves input order in kept[], bounds in-flight items with a
  semaphore, processes in batches so a cancelled() callback stays responsive,
  and returns (kept, dropped, stats). dropped entries are
  {item, reason, stage}.
"""
import asyncio
from typing import Any


class DropItem(Exception):
    def __init__(self, reason: str = "", stage: str = "") -> None:
        super().__init__(reason)
        self.reason = str(reason or "")[:300]
        self.stage = str(stage or "")[:60]


class ItemPipeline:
    def __init__(self, stages: list[tuple[str, Any]], max_concurrency: int = 8) -> None:
        self._stages = [(str(name), fn) for name, fn in stages]
        self._max = max(1, int(max_concurrency or 1))
        self._open = False

    async def open(self) -> None:
        for name, fn in self._stages:
            opener = getattr(fn, "open", None)
            if callable(opener):
                await opener()
        self._open = True

    async def close(self) -> None:
        for name, fn in reversed(self._stages):
            closer = getattr(fn, "close", None)
            if callable(closer):
                await closer()
        self._open = False

    async def _one(self, item: dict, sem: asyncio.Semaphore) -> dict:
        current = item
        async with sem:
            for name, fn in self._stages:
                try:
                    current = await fn(current)
                except DropItem as e:
                    e.stage = e.stage or name
                    raise
        return current

    async def run(self, items: list[dict], cancelled: Any = None) -> tuple[list, list, dict]:
        """Returns (kept, dropped, stats). Order-preserving; cancel-responsive.

        Items flow in batches of 2x max_concurrency so a cancelled() callback
        is honored between batches; in-flight items always drain (bounded).
        """
        if not self._open:
            await self.open()
        kept: list = []
        dropped: list = []
        stats = {"received": len(items), "kept": 0, "dropped": 0}
        sem = asyncio.Semaphore(self._max)
        batch = max(1, self._max * 2)
        try:
            for start in range(0, len(items), batch):
                if callable(cancelled) and cancelled():
                    break
                chunk = items[start:start + batch]
                results = await asyncio.gather(
                    *(self._one(it, sem) for it in chunk), return_exceptions=True)
                for original, res in zip(chunk, results):
                    if isinstance(res, DropItem):
                        dropped.append({"item": original, "reason": res.reason,
                                        "stage": res.stage})
                    elif isinstance(res, BaseException):
                        raise res
                    else:
                        kept.append(res)
        finally:
            await self.close()
        stats["kept"] = len(kept)
        stats["dropped"] = len(dropped)
        return kept, dropped, stats
