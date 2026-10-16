"""API dependencies: correlation IDs + the API-key gate (timing-safe).

The gate FAILS CLOSED. An earlier version returned early when `API_KEY` was
unset, on the reasoning that a missing key means "local dev". That inverts the
failure mode: a container started without a mounted .env silently became an
open proxy that would spend Tavily, Gemini, Groq and OpenRouter budget for
anyone who found the URL, and nothing reported it. Being unreachable is a
recoverable outage; being an open metered proxy is not.

Open local development is an explicit, opt-in decision
(`ALLOW_UNAUTHENTICATED=true`), so it is visible in configuration review rather
than inferred from an absence.

Errors are `AppError`, not `HTTPException`: this is the only module that ever
raised the latter, so auth failures alone arrived as FastAPI's bare
`{"detail": {...}}` while every other error used the envelope the client
parses. A rejected key therefore looked like an unknown failure in the UI.
"""
import hmac
import logging
import uuid
from typing import Annotated, Optional

from fastapi import Header

from app.core.config import settings
from app.core.errors import auth_not_configured, unauthorized

log = logging.getLogger("datagoblin")

#: The header a client may use to supply its own correlation id.
#:
#: Read from config rather than written into the signature as a literal
#: `x_request_id`. `CORRELATION_HEADER` was declared and read by nothing, so
#: changing it had no effect and the parameter name silently disagreed with the
#: setting. FastAPI derives the wire name from the parameter name, so the header
#: this accepts is `X-Request-ID` unless the setting is applied — which is what
#: `_header_param_name` does: it returns the configured name with hyphens turned
#: into underscores, since that is the only spelling FastAPI can bind.
#: Kept for reference and asserted by tests: FastAPI binds the wire header from
#: the explicit `alias=`, so the parameter name above is free.
_CORRELATION_PARAM = settings.CORRELATION_HEADER.strip().replace("-", "_")


async def correlation_id(
    request_id: Annotated[Optional[str], Header(alias=settings.CORRELATION_HEADER)] = None,
) -> str:
    return request_id or str(uuid.uuid4())


_UNAUTH_REASON = (
    "API_KEY is not configured, so every request would be refused. Set API_KEY, "
    "or set ALLOW_UNAUTHENTICATED=true to run an explicitly unauthenticated "
    "local instance."
)


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = settings.API_KEY

    if not expected:
        if settings.ALLOW_UNAUTHENTICATED:
            return
        # Refuse every request, so the misconfiguration is unmissable rather
        # than something you discover in a log later.
        log.error("refusing request: no API_KEY and ALLOW_UNAUTHENTICATED is false")
        raise auth_not_configured(_UNAUTH_REASON)

    # compare_digest, never ==: a byte-by-byte early exit on a secret is a
    # timing oracle. A missing key is also wrong, and cannot be compared.
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise unauthorized()
