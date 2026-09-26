"""JSON structured logging with recursive secret redaction (docs/35 §5)."""
import json
import logging
import sys

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


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
_root = logging.getLogger("datagoblin")
_root.handlers = [_handler]
_root.setLevel(logging.INFO)
_root.propagate = False

log = _root


def set_level(name: str) -> None:
    log.setLevel(getattr(logging, name.upper(), logging.INFO))
