"""Provider error classification — one owner for transient vs fatal.

Shared by the Zen, Groq and Gemini rungs so a 429/timeout always cascades and
an auth/400 always stops: failing over on a fatal error bills a second provider
for a request that is wrong, not unlucky.
"""
import asyncio

from app.core.errors import provider_fatal, provider_transient


def classify(prefix: str, err: Exception) -> Exception:
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
