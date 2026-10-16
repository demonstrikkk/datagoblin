"""A small TTL cache with single-flight, for expensive derived reads.

Built because the measured facts demanded it, not as an optimisation in
search of one:

* `SELECT 1` against the configured database costs **177ms** — every statement
  has that floor, because the pooler is remote.
* `GET /api/datasets/{id}/gaps` issues **6 statements** for 142 records and so
  takes **~2.7s**.
* 16 concurrent callers took **15.7s**, scaling linearly, because the pool
  holds 5 connections and the queue behind them is the whole latency.

Two things therefore matter, and they are different things. **TTL** stops the
identical answer being recomputed on every navigation. **Single-flight** stops
the first request after a cache drop from spawning N identical cold computes at
once — the stampede, which is the expensive case and the one a TTL does not
cover.

The threading model is deliberate and matches `services.dashboard`: the computes
are synchronous and the routes run them through `asyncio.to_thread`, so waiters
block a worker thread rather than the event loop. An asyncio version would have
to move the compute onto the loop to share an awaitable future, which would
block every other request for the duration — a slower way of fixing a stampede
than causing one. The lock is only ever held across dict operations, never
across the factory.

Correctness note: caching a *derived* read is safe only if everything that can
change it invalidates. `invalidate(prefix=...)` is called on every write path,
so this is a correctness dependency, not a performance nicety.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

#: How long a computed read is reused. Generous on purpose: every write that
#: could change the answer drops the cache itself, so a short TTL would only
#: add recomputes.
DEFAULT_TTL_S = 300.0

#: How long a concurrent caller waits for a compute already in flight before
#: doing the work itself. Bounds a waiter, not freshness, so it does not
#: interact with the TTL. On timeout the waiter recomputes rather than failing:
#: two computes are wasteful, a request that never returns is not an answer.
SINGLE_FLIGHT_S = 120.0

_cache: dict[str, tuple[float, Any]] = {}
_inflight: dict[str, dict] = {}
_lock = threading.Lock()


def invalidate(prefix: str = "") -> int:
    """Drop cached reads. `prefix` narrows it to one dataset.

    Called on every write that can change a derived read — an applied backfill,
    an applied refresh, a rejudge, a terminal run. Returns how many entries were
    dropped, which is only used by tests.
    """
    with _lock:
        if not prefix:
            dropped = len(_cache)
            _cache.clear()
            return dropped
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            _cache.pop(k, None)
        return len(keys)


def _single_flight(key: str, factory: Callable[[], Any]) -> tuple[Any, bool]:
    with _lock:
        pending = _inflight.get(key)
        if pending is None:
            pending = {"done": threading.Event(), "value": None,
                       "error": None}
            _inflight[key] = pending
            leader = True
        else:
            leader = False

    if not leader:
        if not pending["done"].wait(timeout=SINGLE_FLIGHT_S):
            return factory(), True
        if pending["error"] is not None:
            raise pending["error"]
        return pending["value"], True

    try:
        value = factory()
    except BaseException as exc:  # noqa: BLE001 (re-raised for every waiter too)
        with _lock:
            _inflight.pop(key, None)
        pending["error"] = exc
        pending["done"].set()
        raise
    with _lock:
        _inflight.pop(key, None)
    pending["value"] = value
    pending["done"].set()
    return value, False


def cached(key: str, factory: Callable[[], Any], ttl_s: float = DEFAULT_TTL_S
           ) -> tuple[Any, bool, bool]:
    """`(key, factory) -> (value, cache_hit, shared)`.

    A failing factory is never cached. Caching an exception would turn one
    transient database blip into a TTL-long outage for that key, which is a much
    worse failure than recomputing and failing again.
    """
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry[0]) < ttl_s:
            return entry[1], True, False

    value, shared = _single_flight(key, factory)
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value, False, shared


def stats() -> dict[str, Any]:
    """Cache shape, for diagnostics. Never exposes a cached payload."""
    now = time.monotonic()
    with _lock:
        live = sum(1 for t, _ in _cache.values() if (now - t) < DEFAULT_TTL_S)
        return {"entries": len(_cache), "live": live,
                "inflight": len(_inflight),
                "ttl_s": DEFAULT_TTL_S,
                "single_flight_s": SINGLE_FLIGHT_S}