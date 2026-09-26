"""LLM: OpenCode server primary -> Groq -> Gemini fallback. Timeout, retry, identity.

Every success returns {"data": <parsed JSON>, "provider": ..., "model": ...}.
Every failure raises AppError (transient for 429/5xx/timeout/unreachable,
fatal for 4xx/auth). The OpenCode rung fails transient when the server is
down, so the pipeline degrades to Groq/Gemini instead of stalling.
"""
import asyncio
import base64
import json
import random
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, provider_fatal, provider_transient


def _classify(prefix: str, err: Exception) -> Exception:
    msg = str(err)
    low = msg.lower()
    if "429" in msg or "rate" in low or "quota" in low or "resource_exhausted" in low:
        return provider_transient(f"{prefix} rate-limited: {msg[:150]}")
    if "401" in msg or "403" in msg or "api key" in low or "permission" in low:
        return provider_fatal(f"{prefix} auth failed: {msg[:150]}")
    if "timeout" in low or isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return provider_transient(f"{prefix} timeout: {msg[:150]}")
    if "400" in msg or "invalid" in low:
        return provider_fatal(f"{prefix} rejected request: {msg[:150]}")
    return provider_transient(f"{prefix} error: {msg[:150]}")


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


async def opencode_structured(prompt: str, schema: dict) -> dict[str, Any]:
    """Local OpenCode server via its session protocol (verified against /doc).

    Flow: POST /api/session -> POST /api/session/{id}/prompt {prompt:{text}}
    -> poll GET .../message for the assistant text -> tolerant JSON parse.
    Optional OPENCODE_MODEL pins the session model first (best-effort).
    Unreachable/auth/stall/timeout/non-JSON => transient (chain falls back).
    Only a confident 4xx-shape refusal is fatal; everything ambiguous fails
    soft so one slow server never stalls the pipeline.
    """
    from app.schemas.evidence import parse_llm_json
    if not settings.OPENCODE_ENABLED:
        raise provider_fatal("OpenCode rung disabled (OPENCODE_ENABLED=false)")
    base = (settings.OPENCODE_BASE_URL or "").rstrip("/")
    if not base:
        raise provider_fatal("OpenCode rung unconfigured (OPENCODE_BASE_URL empty)")
    headers = {"Content-Type": "application/json"}
    if settings.OPENCODE_PASSWORD:
        token = base64.b64encode(f"opencode:{settings.OPENCODE_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    full_prompt = ("Reply with a single JSON object only. No prose, no backticks.\n"
                   + (prompt or ""))[:12000]

    async def _fail_soft(prefix: str, detail: str) -> Exception:
        return provider_transient(f"{prefix}: {detail[:150]}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.post(f"{base}/api/session", json={}, headers=headers)
            except Exception as e:
                raise provider_transient(f"OpenCode unreachable: {str(e)[:120]}")
            if r.status_code in (401, 403):
                raise provider_fatal(f"OpenCode auth failed (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise provider_transient(f"OpenCode session refused (HTTP {r.status_code})")
            try:
                sid = (r.json().get("data", {}) or {}).get("id", "")
            except Exception:
                sid = ""
            if not sid:
                raise provider_transient("OpenCode session created without id")
            if settings.OPENCODE_MODEL:
                try:
                    await client.post(f"{base}/api/session/{sid}/model",
                                      json={"model": settings.OPENCODE_MODEL},
                                      headers=headers)
                except Exception:
                    pass  # pin best-effort; server default otherwise
            try:
                r = await client.post(f"{base}/api/session/{sid}/prompt",
                                      json={"prompt": {"text": full_prompt}},
                                      headers=headers)
            except Exception as e:
                raise provider_transient(f"OpenCode prompt failed: {str(e)[:120]}")
            if r.status_code in (401, 403):
                raise provider_fatal(f"OpenCode auth failed (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise provider_transient(
                    f"OpenCode prompt refused (HTTP {r.status_code}): {r.text[:120]}")
            deadline = asyncio.get_running_loop().time() + settings.OPENCODE_TIMEOUT_S
            last_text = ""
            while True:
                await asyncio.sleep(4)
                try:
                    r = await client.get(f"{base}/api/session/{sid}/message",
                                         params={"limit": 10, "order": "desc"},
                                         headers=headers)
                    msgs = (r.json().get("data", []) or [])
                except Exception:
                    msgs = []
                texts = []
                for m in msgs if isinstance(msgs, list) else []:
                    if m.get("type") != "assistant":
                        continue
                    for p in (m.get("content", []) or []):
                        if isinstance(p, dict) and p.get("type") == "text" \
                                and p.get("text") and not p.get("ignored"):
                            texts.append(p["text"])
                if texts:
                    last_text = "\n".join(texts)
                    try:
                        return {"data": parse_llm_json(last_text), "provider": "opencode",
                                "model": settings.OPENCODE_MODEL or "server-default"}
                    except ValueError:
                        pass  # still streaming / thinking; keep polling
                if asyncio.get_running_loop().time() >= deadline:
                    if last_text:
                        raise provider_transient(
                            f"OpenCode answer never became JSON ({len(last_text)} chars)")
                    raise provider_transient("OpenCode answer timed out")
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001 (anything ambiguous => soft fail)
        raise await _fail_soft("OpenCode error", str(e))


async def structured_generate(prompt: str, schema: dict) -> dict[str, Any]:
    """OpenCode primary -> Groq -> Gemini, transient failures only cascade.

    Fatal errors (auth, bad request) raise immediately — failing over would bill
    a second provider for a request that is wrong, not unlucky. Unexpected
    non-AppError exceptions propagate unmasked.
    """
    from app.core.errors import AppError
    for attempt in (opencode_structured, groq_structured):
        try:
            return await attempt(prompt, schema)
        except AppError as e:
            if e.http != 502:  # transient class only
                raise
    return await gemini_structured(prompt, schema)
