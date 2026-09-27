"""LLM: OpenCode server primary -> Groq -> Gemini fallback. Timeout, retry, identity.

Every success returns {"data": <parsed JSON>, "provider": ..., "model": ...}.
Every failure raises AppError (transient for 429/5xx/timeout/unreachable,
fatal for 4xx/auth). The OpenCode rung fails transient when the server is
down, so the pipeline degrades to Groq/Gemini instead of stalling.

The OpenCode transport itself lives in opencode.py: the legacy `/api/session`
protocol this module used to inline is gone in opencode 1.18.32 (its model-pin
call 400s, and its prompt call only enqueues, forcing 4s of dead polling).
"""
import asyncio
import json
import random
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, provider_fatal, provider_transient
from app.providers.llm import opencode as opencode_svc
from app.providers.llm.classify import classify as _classify


def _interaction_text(dump: dict) -> str:
    """Output text, falling back to model_output step contents (same interaction,
    no new calls). Empty string when the model produced nothing usable."""
    text = (dump.get("output_text") or "").strip()
    if text:
        return text
    parts = []
    for step in dump.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for block in step.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers. Deterministic sanitization only."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        lines = lines[1:] if len(lines) > 1 else []
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# Warm-session bookkeeping moved to opencode.py; these stay as the seam that
# tests and settings reloads already use.
def _reset_session_cache() -> None:
    opencode_svc.reset_session_cache()


async def gemini_structured(prompt: str, schema: dict) -> dict[str, Any]:
    key = settings.GEMINI_API_KEY
    if not key:
        raise provider_fatal("LLM unavailable: GEMINI_API_KEY unset")
    model = settings.GEMINI_EXTRACT_MODEL
    last: Exception | None = None
    for _ in range(settings.LLM_MAX_RETRIES + 1):
        try:
            def _call() -> str:
                from google import genai
                client = genai.Client(api_key=key)
                it = client.interactions.create(
                    model=model, input=prompt,
                    response_format={"type": "text", "mime_type": "application/json",
                                     "schema": schema})
                text = _strip_fences(_interaction_text(it.model_dump()))
                if not text:
                    errs = it.model_dump().get("errors")
                    raise RuntimeError(f"empty model output (errors={errs})")
                return text
            text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=settings.LLM_TIMEOUT_S)
            return {"data": json.loads(text), "provider": "gemini", "model": model}
        except Exception as e:  # noqa: BLE001 (classified below)
            last = _classify("Gemini", e)
            if getattr(last, "http", 502) != 502:
                raise last
    raise last  # type: ignore[misc]


async def groq_structured(prompt: str, inner_schema: dict) -> dict[str, Any]:
    key = settings.GROQ_API_KEY
    if not key:
        raise provider_fatal("LLM fallback unavailable: GROQ_API_KEY unset")
    model = settings.GROQ_FALLBACK_MODEL
    last: Exception | None = None
    # Single-shot SDK (max_retries=0) + OUR bounded retry on transient only:
    # bursts hit free-tier 429s that clear in seconds — one attempt turns a
    # recoverable blip into a dead page. Fatal (auth/400) raises at once.
    for attempt in range(3):
        try:
            def _call() -> str:
                from groq import Groq
                # json_object mode (not strict json_schema): gpt-oss models reject
                # additionalProperties:false on open object items. The caller
                # validates/coerces content downstream (extractor.coerce_output).
                resp = Groq(api_key=key, max_retries=0, timeout=25).chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"})
                return resp.choices[0].message.content or "{}"
            text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=settings.LLM_TIMEOUT_S)
            _ = inner_schema  # Groq enforces the fixed records envelope; caller validates content.
            return {"data": json.loads(text), "provider": "groq", "model": model}
        except Exception as e:  # noqa: BLE001
            err = _classify("Groq", e)
            if getattr(err, "http", 502) != 502:
                raise err
            last = err
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1) + random.uniform(0, 2))
    raise last  # type: ignore[misc]


async def opencode_structured(prompt: str, schema: dict,
                              model: str | None = None) -> dict[str, Any]:
    """Local OpenCode server, pinned model, one synchronous round trip.

    `model` pins a specific model; without it OPENCODE_MODEL is used. The pin
    travels in the message body, and the reply's `info.modelID` decides which
    model is reported, so a silent server-side fallback surfaces as
    `model_mismatch` instead of being relabelled as the requested model.
    """
    from app.schemas.evidence import parse_llm_json
    full_prompt = ("Reply with a single JSON object only. No prose, no backticks.\n"
                   + (prompt or ""))[:12000]
    out = await opencode_svc.chat(model or settings.OPENCODE_MODEL, full_prompt)
    try:
        data = parse_llm_json(out["text"])
    except ValueError as e:
        raise provider_transient(
            f"OpenCode answer never became JSON ({len(out['text'])} chars): {str(e)[:100]}")
    return {"data": data, "provider": "opencode", "model": out["model"],
            "model_requested": out.get("model_requested"),
            "model_mismatch": bool(out.get("model_mismatch")),
            "latency_ms": out.get("latency_ms", 0), "cost": out.get("cost")}


async def structured_generate(prompt: str, schema: dict) -> dict[str, Any]:
    """OpenCode primary -> Groq -> Gemini, transient failures only cascade.

    OPENCODE_STRICT=true makes opencode THE reader: its result (or error) is
    final and cloud fallbacks never fire — no surprise quota burn. Fatal
    errors (auth, bad request) raise immediately in both modes — failing over
    would bill a second provider for a request that is wrong, not unlucky.
    Unexpected non-AppError exceptions propagate unmasked.
    """
    from app.core.errors import AppError
    if settings.OPENCODE_STRICT:
        return await opencode_structured(prompt, schema)
    for attempt in (opencode_structured, groq_structured):
        try:
            return await attempt(prompt, schema)
        except AppError as e:
            if e.http != 502:  # transient class only
                raise
    return await gemini_structured(prompt, schema)
