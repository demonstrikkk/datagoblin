"""OpenCode Zen — direct REST transport + the free-model registry.

Zen (https://opencode.ai/zen/v1) is one key in front of many providers, and it
speaks three request shapes: `/chat/completions` (OpenAI), `/responses`
(OpenAI Responses), `/messages` (Anthropic) and `/systemone` (Jev decisions).

FREE-TIER GATE — measured, not assumed (2026-09-27, real key, this machine):

    space-bunny-free      direct /chat/completions   200
    every other free text model  direct               403 FreeTierError
    jev-1.13-free        direct /systemone           200

    {"error":{"type":"FreeTierError","message":"OpenCode's free tier can
              only be used from within OpenCode"}}

So a "call every free model" feature cannot be one transport. Each model in
FREE_MODELS carries how it may be reached, and `ask()` dispatches on that:

    direct    -> this module (one HTTP round trip)
    opencode  -> llm/opencode.py (the only sanctioned path for gated models)
    systemone -> this module, /systemone (typed decisions, not prose)

Why a curated registry instead of `GET /models`: the live catalogue returns 82
models and every `cost` is null, so "free" cannot be derived from the payload —
a filter on missing cost would happily select ~80 paid models. The free set is
also not stable (big-pickle is free "for a limited time"), so `FREE_MODELS` is
the source of truth and `available()` reports live reachability on top of it.

`observed` is a POINT-IN-TIME note, not a verdict, and free-tier availability
rotates: across two fan-out runs on the same day, `mimo-v2.5-free` and
`muse-spark-1.2-contributor-free` failed with upstream 500s in the first and
answered correctly in the second, while `ling-3.0-flash-fin-free` and
`nemotron-3-ultra-free` did the reverse. The UI therefore treats `observed` as
advisory and the per-request error as the truth — which is exactly why fan-out
isolates failures per model instead of failing the batch.
"""
import asyncio
import json
import random
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, provider_fatal, provider_transient, validation
from app.core.logging import log
from app.providers.llm.classify import classify

#: Date the `observed` notes below were taken. Advisory only — see module docstring.
OBSERVED_ON = "2026-09-27"

#: The free tier. `data` is that model's Zen privacy posture, which the UI
#: surfaces: private = zero-retention/no-training, may-train = prompts may be
#: used to improve the model, trial = provider trial terms.
FREE_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "space-bunny-free", "transport": "direct", "vendor": "stealth",
     "data": "private", "observed": f"ok {OBSERVED_ON}",
     "note": "only free text model reachable without the OpenCode server"},
    {"id": "big-pickle", "transport": "opencode", "vendor": "stealth",
     "data": "may-train", "observed": f"ok {OBSERVED_ON}",
     "note": "free for a limited time"},
    {"id": "longcat-2.5-preview-free", "transport": "opencode", "vendor": "meituan",
     "data": "private", "observed": f"ok {OBSERVED_ON}",
     "note": "zero-retention provider"},
    {"id": "mimo-v2.6-flash-free", "transport": "opencode", "vendor": "xiaomi",
     "data": "may-train", "observed": f"ok {OBSERVED_ON}", "note": ""},
    {"id": "mimo-v2.5-free", "transport": "opencode", "vendor": "xiaomi",
     "data": "may-train", "observed": f"intermittent {OBSERVED_ON}",
     "note": "upstream 500s seen on some attempts"},
    {"id": "ling-3.0-flash-fin-free", "transport": "opencode", "vendor": "ant",
     "data": "may-train", "observed": f"intermittent {OBSERVED_ON}",
     "note": "upstream 500s seen on some attempts"},
    {"id": "nemotron-3-ultra-free", "transport": "opencode", "vendor": "nvidia",
     "data": "trial", "observed": f"intermittent {OBSERVED_ON}",
     "note": "NVIDIA trial terms; do not send confidential data"},
    {"id": "nemotron-3.5-lightning-free", "transport": "opencode", "vendor": "nvidia",
     "data": "trial", "observed": f"ok {OBSERVED_ON}",
     "note": "NVIDIA trial terms; do not send confidential data"},
    {"id": "muse-spark-1.3-contributor-free", "transport": "opencode",
     "vendor": "muse", "data": "contributor", "observed": f"ok {OBSERVED_ON}",
     "note": "discounted tier traded for training consent"},
    {"id": "muse-spark-1.2-contributor-free", "transport": "opencode",
     "vendor": "muse", "data": "contributor", "observed": f"intermittent {OBSERVED_ON}",
     "note": "upstream 500s seen on some attempts"},
    {"id": "jev-1.13-free", "transport": "systemone", "vendor": "typesafe",
     "data": "private", "observed": f"ok {OBSERVED_ON}", "kind": "decision",
     "note": "typed decisions, not prose generation"},
)

_BY_ID = {m["id"]: m for m in FREE_MODELS}
_TEXT_MODELS = [m["id"] for m in FREE_MODELS if m.get("kind") != "decision"]


def free_models() -> list[dict[str, Any]]:
    """Registry copy — callers may annotate it (e.g. with live reachability)."""
    return [dict(m) for m in FREE_MODELS]


def model_info(model: str) -> dict[str, Any] | None:
    return dict(_BY_ID[model]) if model in _BY_ID else None


def is_free(model: str) -> bool:
    return model in _BY_ID


def text_models() -> list[str]:
    return list(_TEXT_MODELS)


def configured_models() -> list[str]:
    """ZEN_FANOUT_MODELS when set, else every free text model."""
    raw = (settings.ZEN_FANOUT_MODELS or "").strip()
    if not raw:
        return list(_TEXT_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def _key() -> str:
    return (settings.ZEN_API_KEY or "").strip()


def _base() -> str:
    return (settings.ZEN_BASE_URL or "").rstrip("/")


def _preflight() -> None:
    if not settings.ZEN_ENABLED:
        raise provider_fatal("Zen rung disabled (ZEN_ENABLED=false)")
    if not _key():
        raise provider_fatal("Zen rung unconfigured (ZEN_API_KEY unset)")
    if not _base():
        raise provider_fatal("Zen rung unconfigured (ZEN_BASE_URL empty)")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


def _extract_text(payload: dict) -> str:
    """chat/completions, responses and messages shapes in one place."""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # gateways that return content parts
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if msg.get("reasoning_content"):
            return str(msg["reasoning_content"])
    if payload.get("output_text"):
        return str(payload["output_text"])
    blocks = payload.get("content")
    if isinstance(blocks, list):  # anthropic shape
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return ""


def _usage(payload: dict) -> dict:
    u = payload.get("usage") or {}
    return {"input": u.get("prompt_tokens", u.get("input_tokens", 0)) or 0,
            "output": u.get("completion_tokens", u.get("output_tokens", 0)) or 0}


def _http_error(model: str, status: int, body: str) -> AppError:
    """The 403 gate gets a named error: callers dispatch on transport, and
    "gated" is a routing fact, not a credential problem."""
    low = (body or "").lower()
    if "freetiererror" in low.replace(" ", ""):
        return provider_fatal(
            f"Zen {model}: free tier is gated to OpenCode — use the opencode "
            f"transport, not direct REST", {"transport": "opencode", "model": model})
    if status in (401, 403):
        return provider_fatal(f"Zen {model} auth failed (HTTP {status})")
    if status == 429:
        return provider_transient(f"Zen {model} rate-limited (HTTP 429)")
    if status >= 500:
        return provider_transient(f"Zen {model} server error (HTTP {status})")
    return provider_fatal(f"Zen {model} refused request (HTTP {status}): {body[:140]}")


def _gated(err: Exception) -> bool:
    details = getattr(err, "details", None)
    return bool(isinstance(details, dict) and details.get("transport") == "opencode")


async def chat(model: str, prompt: str, *, system: str | None = None,
               temperature: float = 0.0, max_tokens: int | None = None,
               json_mode: bool = False, timeout: float | None = None) -> dict[str, Any]:
    """One DIRECT Zen call. For gated models this raises a named error that
    points at the opencode transport — use `ask()` to dispatch automatically.

    Returns {"text","model","provider","latency_ms","usage","cost"}.
    """
    _preflight()
    info = _BY_ID.get(model)
    if info and info["transport"] == "systemone":
        raise validation(f"{model} is a decision model: use systemone()")
    if info and info["transport"] == "opencode":
        raise provider_fatal(
            f"Zen {model}: free tier is gated to OpenCode — use the opencode "
            f"transport, not direct REST", {"transport": "opencode", "model": model})

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt or ""})
    body: dict[str, Any] = {"model": model, "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens or settings.ZEN_MAX_TOKENS}
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last: Exception | None = None
    for attempt in range(settings.ZEN_MAX_RETRIES + 1):
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout or settings.ZEN_TIMEOUT_S) as c:
                r = await c.post(f"{_base()}/chat/completions", headers=_headers(),
                                 json=body)
            if r.status_code >= 400:
                raise _http_error(model, r.status_code, r.text)
            payload = r.json()
            text = _extract_text(payload)
            if not text.strip():
                raise RuntimeError(f"empty completion (keys={sorted(payload)[:8]})")
            log.info("zen direct", extra={"data": {"model": model,
                                                   "ms": int((time.perf_counter() - started) * 1000)}})
            return {"text": text, "model": model, "provider": "zen",
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "usage": _usage(payload), "cost": payload.get("usage", {}).get("cost")}
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except AppError as e:
            if e.http != 502:  # fatal: the request is wrong, not unlucky
                raise
            last = e
        except Exception as e:  # noqa: BLE001 (classified below)
            classified = classify(f"Zen {model}", e)
            if classified.http != 502:
                raise classified
            last = classified
        if attempt < settings.ZEN_MAX_RETRIES:
            await asyncio.sleep(2 * (attempt + 1) + random.uniform(0, 1.5))
    raise last  # type: ignore[misc]


async def ask(model: str, prompt: str, **kw) -> dict[str, Any]:
    """Transport-aware entry point: pick the legal path for this model.

    Direct when Zen allows it, OpenCode server when the free tier is gated.
    Every caller (fan-out, Jev, the API) goes through here so no code path can
    accidentally hit a 403 loop against a gated model.
    """
    info = _BY_ID.get(model)
    transport = (info or {}).get("transport", "direct")
    if transport == "opencode":
        from app.providers.llm import opencode as opencode_svc
        # The gated free tier is flaky by nature: measured, a live 4-model
        # fan-out lost one model to a bare HTTP 500 that a retry answered
        # immediately. Without a retry a single upstream blip permanently
        # shrinks coverage (8/10 shown as "65%"), so transient failures get
        # another go. Fatal ones (auth, refused request) still fail fast —
        # retrying those only burns the budget.
        last: Exception | None = None
        for attempt in range(settings.ZEN_MAX_RETRIES + 1):
            try:
                out = await opencode_svc.chat(model, prompt,
                                              system=kw.get("system"),
                                              timeout=kw.get("timeout"))
                return {"text": out["text"], "model": out["model"],
                        "model_requested": out.get("model_requested"),
                        "model_mismatch": bool(out.get("model_mismatch")),
                        "provider": "opencode",
                        "attempts": attempt + 1,
                        "latency_ms": out["latency_ms"], "usage": out["tokens"],
                        "cost": out.get("cost"), "transport": "opencode"}
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except AppError as e:
                if e.http != 502:  # fatal: the request is wrong, not unlucky
                    raise
                # A refused connection means the local server is not running.
                # Retrying that cannot succeed and just multiplies the stall -
                # measured: a fan-out against a stopped server took 61s instead
                # of 19s and still answered nothing. Fail fast and say so.
                if e.message.startswith("OpenCode unreachable"):
                    raise
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
            if attempt < settings.ZEN_MAX_RETRIES:
                log.info("zen opencode retry",
                         extra={"data": {"model": model, "attempt": attempt + 1,
                                         "of": settings.ZEN_MAX_RETRIES,
                                         "error": str(last)[:120]}})
                await asyncio.sleep(2 * (attempt + 1) + random.uniform(0, 1.5))
        # The attempt count is part of the diagnosis: "HTTP 500" alone cannot
        # distinguish one unlucky call from a model that is simply unavailable.
        raise provider_transient(
            f"{last} (after {settings.ZEN_MAX_RETRIES + 1} attempts)",
            {"model": model, "attempts": settings.ZEN_MAX_RETRIES + 1,
             "transport": "opencode"})
    if transport == "systemone":
        raise validation(f"{model} is a decision model: use systemone()")
    out = await chat(model, prompt, **kw)
    out["transport"] = "direct"
    return out


async def structured(prompt: str, schema: dict, *, model: str | None = None,
                     timeout: float | None = None) -> dict[str, Any]:
    """JSON-mode call parsed into the caller's contract."""
    from app.schemas.evidence import parse_llm_json
    used = model or settings.ZEN_MODEL
    out = await chat(used, "Reply with a single JSON object only. No prose, "
                           "no backticks.\n" + (prompt or ""),
                     json_mode=True, timeout=timeout)
    try:
        data = parse_llm_json(out["text"])
    except ValueError as e:
        raise provider_transient(
            f"Zen {used} answer never became JSON ({len(out['text'])} chars): "
            f"{str(e)[:100]}")
    _ = schema  # free endpoints honour json_object, not per-call json_schema
    return {"data": data, "provider": "zen", "model": used,
            "latency_ms": out["latency_ms"], "usage": out["usage"]}


class JevRateLimited(Exception):
    """The judge answered 429: throttled, not absent.

    Raised rather than folded into "unavailable" so the run can tell the
    difference between *nothing to judge* and *told to wait*. The distinction
    matters operationally: a throttled judge is worth backing off and retrying,
    and a run that was throttled should say so instead of reporting its fields
    as simply unverified.
    """


#: Live counters, surfaced by /api/health so throttling is observable rather
#: than something you notice in a log 400 lines later.
JEV_HEALTH: dict[str, int] = {"calls": 0, "rate_limited": 0, "unavailable": 0,
                              "retried": 0, "succeeded": 0}


async def systemone(state: str, questions: dict, *,
                    model: str | None = None,
                    retries: int | None = None) -> dict | None:
    """Jev typed decisions over Zen. {"answers": {...}} or None.

    None means unavailable (no key, refusal, transport error) so the caller
    keeps its deterministic policy — never a fabricated judgment. A 429 is
    different and raises `JevRateLimited` after bounded exponential backoff:
    throttling is a "wait", not a "no".
    """
    if not settings.ZEN_ENABLED or not settings.ZEN_JEV_ENABLED or not _key():
        JEV_HEALTH["unavailable"] += 1
        return None
    body = {"model": model or settings.JEV_ZEN_MODEL,
            "state": (state or "")[:30000], "questions": questions}
    attempts = max(0, int(retries if retries is not None
                          else settings.JEV_MAX_RETRIES))
    JEV_HEALTH["calls"] += 1
    for attempt in range(attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=min(settings.ZEN_TIMEOUT_S, 60)) as c:
                r = await c.post(f"{_base()}/systemone", headers=_headers(), json=body)
            if r.status_code == 429:
                JEV_HEALTH["rate_limited"] += 1
                if attempt < attempts:
                    # Honour Retry-After when the server states it; otherwise
                    # back off. Quitting on the first 429 is what made a busy
                    # run report every field as unverified.
                    delay = _retry_after_s(r) or (2 ** attempt)
                    JEV_HEALTH["retried"] += 1
                    log.info("zen jev throttled; backing off",
                             extra={"data": {"status": 429, "model": body["model"],
                                             "attempt": attempt + 1,
                                             "delay_s": round(delay, 2)}})
                    await asyncio.sleep(delay)
                    continue
                raise JevRateLimited(
                    f"judge throttled (HTTP 429) after {attempts + 1} attempts")
            if r.status_code >= 400:
                log.info("zen jev refused", extra={"data": {"status": r.status_code,
                                                           "model": body["model"]}})
                JEV_HEALTH["unavailable"] += 1
                return None
            JEV_HEALTH["succeeded"] += 1
            return (r.json() or {}).get("answers") or None
        except JevRateLimited:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as e:  # noqa: BLE001 (judge degrades to deterministic policy)
            log.info("zen jev failed", extra={"data": {"error": str(e)[:150]}})
            JEV_HEALTH["unavailable"] += 1
            return None
    return None


def _retry_after_s(r: Any) -> float | None:
    """Server-stated cool-down, capped so a hostile value cannot stall a run."""
    raw = (r.headers or {}).get("Retry-After") if hasattr(r, "headers") else None
    if not raw:
        return None
    try:
        return min(30.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


def parse_json_lenient(text: str) -> Any | None:
    """Best-effort JSON from a model answer. Never raises; None when unusable."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            return None
    return None
