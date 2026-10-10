"""Structured logging with request/event context carried in ``contextvars``.

Log records are emitted as one JSON object per line. Field names follow what
Cloud Logging parses natively (``severity``, ``message``), so no paid agent or
custom metric is required. Payload bodies and secret values are never logged;
callers log identifiers and classifications only.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_context: ContextVar[dict[str, Any]] = ContextVar("relay_log_context", default={})  # noqa: B039

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


def get_log_context() -> dict[str, Any]:
    return dict(_context.get())


def correlation_id() -> str | None:
    value = _context.get().get("correlation_id")
    return str(value) if value else None


@contextmanager
def bind_log_context(**values: Any) -> Iterator[None]:
    """Attach key/value pairs to every log line emitted inside the block."""
    merged = {**_context.get(), **{k: v for k, v in values.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(_context.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {
            **_context.get(),
            **{
                k: v
                for k, v in record.__dict__.items()
                if k not in _RESERVED and not k.startswith("_")
            },
        }
        suffix = " ".join(f"{k}={v}" for k, v in extras.items())
        line = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if suffix:
            line = f"{line}  [{suffix}]"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates our own request log and omits correlation IDs.
    logging.getLogger("uvicorn.access").handlers[:] = []
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
