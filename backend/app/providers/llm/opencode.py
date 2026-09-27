"""OpenCode server transport — the only sanctioned path to gated Zen models.

Measured 2026-09-27 against opencode 1.18.32. The old `/api/*` session flow in
generate.py is gone because two of its assumptions are false on this version:

  * `POST /api/session/{id}/model` answers 400 and the caller swallowed the
    error, so `OPENCODE_MODEL` was silently NEVER applied — the server default
    answered every request.
  * `POST /api/session/{id}/prompt` only enqueues a steer; the answer arrives by
    polling, so every call cost a fixed 4s dead wait before the first byte.

The v1 flow is synchronous and pins the model per message:

    POST /session                        -> {"id": ...}
    POST /session/{id}/message            -> {"info": {...}, "parts": [...]}
        body: {"model": {"providerID": "opencode", "modelID": "<id>"},
               "parts": [{"type": "text", "text": "..."}]}
    DELETE /session/{id}                  -> session teardown

`info.modelID` in the reply is the model that actually answered, so the caller
never has to trust its own request. A reply naming a different model than the
one pinned is reported as `model_mismatch` rather than hidden, so a silent
fallback shows up in the result instead of quietly relabelling the answer.

Sessions are pooled, not shared. A single warm session id handed to concurrent
callers is unsafe on this server: four concurrent messages posted to one session
were all served by that session's pinned model and every caller received the same
reply, which would have made two run workers record each other's extractions. A
session is now owned by exactly one in-flight call and recycled afterwards, so
warm-start is kept and cross-serving is not. Creation stays locked so a parallel
fan-out cannot stampede session creation.
"""
import asyncio
import base64
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, provider_fatal, provider_transient

#: Idle session pool (LIFO). Bounded by _SESSION_POOL_MAX; entries are retired
#: after _SESSION_MAX_USES or _SESSION_TTL_S.
_session_pool: list[dict] = []
_SESSION_POOL_MAX = 4
_SESSION_MAX_USES = 6
_SESSION_TTL_S = 300.0
_open_lock = asyncio.Lock()


def _reset_session_cache() -> None:
    _session_pool.clear()


def reset_session_cache() -> None:
    """Public alias (tests + config reloads)."""
    _reset_session_cache()


def _base() -> str:
    return (settings.OPENCODE_BASE_URL or "").rstrip("/")


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.OPENCODE_PASSWORD:
        token = base64.b64encode(
            f"opencode:{settings.OPENCODE_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


def _preflight() -> None:
    """Rung disabled / unconfigured are fatal configuration, not a blip."""
    if not settings.OPENCODE_ENABLED:
        raise provider_fatal("OpenCode rung disabled (OPENCODE_ENABLED=false)")
    if not _base():
        raise provider_fatal("OpenCode rung unconfigured (OPENCODE_BASE_URL empty)")


def _split_model(spec: str) -> tuple[str, str | None]:
    """Accept both `model-id` and the `provider/model-id` form.

    Existing .env files carry `OPENCODE_MODEL=opencode/muse-spark-1.3-contributor-free`
    (that is how opencode's own config addresses Zen models), but the v1 wire
    format wants the provider and the model as separate fields. Sending the
    prefixed string as `modelID` 404s / silently falls back, so the prefix is
    split off here and the provider is honoured.
    """
    spec = (spec or "").strip()
    if "/" in spec:
        provider, _, model = spec.partition("/")
        if provider and model:
            return model, provider
    return spec, None


def _part_text(parts: Any) -> str:
    """Assistant text from a v1 message reply.

    `synthetic` parts are server-generated scaffolding (tool plumbing, notices),
    not the model's answer, so they are excluded.
    """
    out: list[str] = []
    for p in parts if isinstance(parts, list) else []:
        if not isinstance(p, dict) or p.get("type") != "text":
            continue
        if p.get("synthetic") or p.get("ignored"):
            continue
        text = p.get("text")
        if text:
            out.append(str(text))
    return "\n".join(out).strip()


async def _delete_session(client: Any, sid: str, headers: dict) -> None:
    try:
        await client.delete(f"{_base()}/session/{sid}", headers=headers)
    except Exception:  # noqa: BLE001 (hygiene only; sessions expire server-side)
        pass


async def _drop_session(client: Any, sid: str, headers: dict) -> None:
    """Delete and evict — for a call that failed, so nothing broken is recycled."""
    _session_pool[:] = [e for e in _session_pool if e["sid"] != sid]
    await _delete_session(client, sid, headers)


async def _acquire_session(client: Any, headers: dict) -> tuple[str, bool]:
    """Check a session out EXCLUSIVELY. Returns (sid, fresh).

    A single shared session id is not safe under concurrency. Measured against a
    live server: four concurrent messages posted to one session were all served
    by whichever model that session was pinned to, and all four callers received
    the *same* reply. Two run workers extracting two different pages would
    silently record each other's records. A session is therefore owned by exactly
    one in-flight call and returned to the idle pool only when that call ends.
    """
    loop = asyncio.get_running_loop()
    async with _open_lock:
        now = loop.time()
        while _session_pool:
            entry = _session_pool.pop()
            if (entry["uses"] < _SESSION_MAX_USES
                    and now - entry["at"] < _SESSION_TTL_S):
                entry["uses"] += 1
                entry["at"] = now
                return entry["sid"], False
            await _delete_session(client, entry["sid"], headers)  # stale
        try:
            r = await client.post(f"{_base()}/session", json={}, headers=headers)
        except Exception as e:  # noqa: BLE001
            raise provider_transient(f"OpenCode unreachable: {str(e)[:120]}")
        if r.status_code in (401, 403):
            raise provider_fatal(f"OpenCode auth failed (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise provider_transient(f"OpenCode session refused (HTTP {r.status_code})")
        try:
            new_sid = (r.json() or {}).get("id", "")
        except Exception:  # noqa: BLE001
            new_sid = ""
        if not new_sid:
            raise provider_transient("OpenCode session created without id")
        return new_sid, True


async def _release_session(client: Any, sid: str, headers: dict) -> None:
    """Return a healthy session to the idle pool; delete it if there is no room."""
    loop = asyncio.get_running_loop()
    async with _open_lock:
        if len(_session_pool) < _SESSION_POOL_MAX:
            _session_pool.append({"sid": sid, "uses": 1, "at": loop.time()})
            return
    await _delete_session(client, sid, headers)


def _status_error(status: int, body: str) -> Exception:
    """4xx = the request is wrong (stop). 5xx/429 = unlucky (cascade)."""
    if status in (401, 403):
        return provider_fatal(f"OpenCode auth failed (HTTP {status})")
    if status == 404:
        # Measured, do not speculate. A 404 here is NOT a version mismatch: the
        # same endpoint answered 200 for every other model in the same run, and a
        # genuinely unknown model id returns 500 UnknownError, not 404. Read as
        # "this model was not served on this request" and retryable, because
        # that is the only cause actually observed - a claim of a version
        # mismatch here is an unearned guess that sends people to debug the
        # wrong thing.
        return provider_transient(
            f"OpenCode did not serve this model on this request (HTTP 404). "
            f"The endpoint itself is reachable; treat as a retryable miss.")
    if status == 429:
        return provider_transient(f"OpenCode rate-limited (HTTP 429)")
    if status >= 500:
        return provider_transient(f"OpenCode server error (HTTP {status}): {body[:120]}")
    return provider_fatal(f"OpenCode refused request (HTTP {status}): {body[:120]}")


async def chat(model: str, prompt: str, *, system: str | None = None,
               timeout: float | None = None) -> dict[str, Any]:
    """One pinned, synchronous OpenCode call.

    Returns {"text", "model", "provider", "latency_ms", "cost", "tokens"}.
    Raises AppError — fatal for auth/404/4xx, transient for 5xx/429/timeout —
    so the caller's cascade behaviour stays correct.
    """
    _preflight()
    # The REQUESTED model wins; OPENCODE_MODEL is only the fallback.
    # Regression, measured: `settings.OPENCODE_MODEL or model` silently pinned
    # every call to muse-spark-1.3, so a fan-out asking for four different
    # models got four answers from that one model — the registry, the
    # per-model provenance and the agreement score were all fiction.
    model, spec_provider = _split_model(model or settings.OPENCODE_MODEL)
    if not model:
        raise provider_fatal("OpenCode rung unconfigured (OPENCODE_MODEL empty)")
    provider = spec_provider or settings.OPENCODE_PROVIDER_ID or "opencode"

    parts: list[dict] = []
    if system:
        parts.append({"type": "text", "text": system})
    parts.append({"type": "text", "text": prompt or ""})
    body = {"model": {"providerID": provider, "modelID": model},
            "parts": parts}
    headers = _headers()
    limit = timeout or settings.OPENCODE_TIMEOUT_S
    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(limit, connect=10)) as client:
            sid, _fresh = await _acquire_session(client, headers)
            try:
                r = await client.post(f"{_base()}/session/{sid}/message",
                                      json=body, headers=headers)
            except Exception as e:  # noqa: BLE001
                await _drop_session(client, sid, headers)
                raise provider_transient(f"OpenCode message failed: {str(e)[:120]}")
            if r.status_code >= 400:
                await _drop_session(client, sid, headers)
                raise _status_error(r.status_code, r.text)
            try:
                payload = r.json() or {}
            except Exception as e:  # noqa: BLE001
                await _drop_session(client, sid, headers)
                raise provider_transient(f"OpenCode reply not JSON: {str(e)[:100]}")

            info = payload.get("info") or {}
            # 200 can still carry a model-side failure.
            if info.get("error"):
                await _drop_session(client, sid, headers)
                raise provider_transient(
                    f"OpenCode model error: {str(info['error'])[:140]}")
            text = _part_text(payload.get("parts"))
            if not text:
                await _drop_session(client, sid, headers)
                raise provider_transient("OpenCode returned no text")
            tokens = info.get("tokens") or {}
            # Provenance is taken from the server, never from the request. A
            # silent fallback is reported, not hidden and not thrown away:
            # dropping the answer would cost real coverage, and labelling it
            # with the requested model would make the fan-out agreement score
            # a fiction (one model asked four times looks like 4/4 consensus).
            served = info.get("modelID") or model
            await _release_session(client, sid, headers)
            return {"text": text,
                    "model": served,
                    "model_requested": model,
                    "model_mismatch": bool(served and served != model),
                    "provider": info.get("providerID") or provider,
                    "cost": info.get("cost"),
                    "tokens": {"input": tokens.get("input", 0),
                               "output": tokens.get("output", 0),
                               "reasoning": tokens.get("reasoning", 0)},
                    "latency_ms": int((time.perf_counter() - started) * 1000)}
    except asyncio.CancelledError:
        raise
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001 (anything ambiguous fails soft)
        raise provider_transient(f"OpenCode error: {str(e)[:150]}")


async def available() -> dict[str, Any]:
    """Cheap health probe for the model-registry endpoint. Never raises."""
    out: dict[str, Any] = {"reachable": False, "base_url": _base(),
                           "enabled": bool(settings.OPENCODE_ENABLED),
                           "error": ""}
    if not out["enabled"]:
        out["error"] = "disabled"
        return out
    if not _base():
        out["error"] = "unconfigured"
        return out
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_base()}/session", headers=_headers())
        out["reachable"] = r.status_code < 500
        out["http_status"] = r.status_code
        if not out["reachable"]:
            out["error"] = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:120]
    return out
