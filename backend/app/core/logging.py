"""JSON structured logging with recursive secret redaction (docs/35 §5).

`LOG_JSON` chooses the formatter. It used to be declared in config and read by
nothing: `_JsonFormatter` was installed unconditionally, so setting `LOG_JSON=false`
did nothing at all. That matters more than it sounds — JSON-per-line is the
right shape for a machine-readable log and actively unpleasant for a human
reading a terminal, and an operator who set the flag expected the readable one.
"""
import json
import logging
import sys

from app.core.config import settings

_REDACT_KEYS = {"api_key", "apikey", "authorization", "secret", "token", "password"}
_REDACT_SUFFIXES = ("_key", "_secret", "_token", "authorization")


def _redact(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS or k.lower().endswith(_REDACT_SUFFIXES) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "name": record.name,
                   "msg": record.getMessage()}
        if isinstance(getattr(record, "data", None), dict):
            payload["data"] = _redact(record.data)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Readable single-line form for a terminal.

    Keeps the `extra={"data": ...}` payload, because that is where the useful
    detail lives; it just stops forcing every consumer to parse JSON to read it.
    """

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                         datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data:
            return f"{base}  {_redact(data)}"
        return base


def _build_handler(json_mode: bool) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_mode else _HumanFormatter())
    return handler


_root = logging.getLogger("datagoblin")
_root.handlers = [_build_handler(bool(getattr(settings, "LOG_JSON", True)))]
_root.setLevel(logging.INFO)
_root.propagate = False

log = _root


def set_level(name: str) -> None:
    log.setLevel(getattr(logging, name.upper(), logging.INFO))


def set_json_mode(enabled: bool) -> None:
    """Swap the formatter. Called once at startup from `main`, and by tests."""
    _root.handlers = [_build_handler(bool(enabled))]
