"""Coded error taxonomy (docs/35 §3). Stable machine codes; transport maps to HTTP."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AppError(Exception):
    code: str
    message: str
    http: int = 500
    details: Any = None

    def envelope(self, correlation_id: str = "") -> dict:
        return {"data": None, "error": {"code": self.code, "message": self.message,
                                        "details": self.details or {}},
                "meta": {"correlation_id": correlation_id}}


# Factory helpers (single construction site; no exception sprawl)
def validation(msg: str, details: Any = None) -> AppError:
    return AppError("E_VALIDATION", msg, 422, details)


def budget(msg: str, details: Any = None) -> AppError:
    return AppError("E_BUDGET", msg, 429, details)


def provider_transient(msg: str, details: Any = None) -> AppError:
    return AppError("E_PROVIDER_TRANSIENT", msg, 502, details)


def provider_fatal(msg: str, details: Any = None) -> AppError:
    return AppError("E_PROVIDER_FATAL", msg, 424, details)


def dependency(msg: str, details: Any = None) -> AppError:
    return AppError("E_DEPENDENCY", msg, 503, details)


def cancelled(msg: str = "Run cancelled") -> AppError:
    return AppError("E_CANCELLED", msg, 499)


def not_found(resource: str, ident: str) -> AppError:
    return AppError("E_NOT_FOUND", f"{resource} {ident} not found", 404)
