"""Coded error taxonomy (docs/35 §3). Stable machine codes; transport maps to HTTP."""
from dataclasses import FrozenInstanceError, dataclass
from typing import Any

#: Slots the interpreter (and contextlib) assign on exception instances. A
#: frozen dataclass overrides __setattr__ for EVERY attribute, so these writes
#: raised FrozenInstanceError instead of doing their job.
_EXCEPTION_SLOTS = ("__traceback__", "__cause__", "__context__",
                    "__suppress_context__", "__notes__")


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


def _app_error_setattr(self: AppError, name: str, value: Any) -> None:
    """Installed after class creation: dataclasses refuses to build a frozen
    class that already defines __setattr__ (dataclasses.py:1080).

    contextlib re-throws an exception into a @contextmanager generator and then
    assigns exc.__traceback__. The stock frozen __setattr__ rejects EVERY write,
    so that assignment raised FrozenInstanceError and REPLACED the real error —
    reproducible with pytest's own logging/capture contextmanagers, and a
    landmine for any future `with` block in the error path. Exception slots go
    to BaseException; the four dataclass fields stay frozen.
    """
    if name in _EXCEPTION_SLOTS:
        BaseException.__setattr__(self, name, value)
        return
    if name in ("code", "message", "http", "details"):
        raise FrozenInstanceError(f"cannot assign to field {name!r}")
    object.__setattr__(self, name, value)


AppError.__setattr__ = _app_error_setattr


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
