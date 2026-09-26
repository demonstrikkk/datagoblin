"""API dependencies: correlation IDs + optional API-key gate (timing-safe)."""
import hmac
import uuid

from fastapi import Header, HTTPException

from app.core.config import settings


async def correlation_id(x_request_id: str | None = Header(default=None)) -> str:
    return x_request_id or str(uuid.uuid4())


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = settings.API_KEY
    if not expected:
        return  # open local dev
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail={"code": "E_UNAUTHORIZED",
                                                     "message": "Invalid API key"})
