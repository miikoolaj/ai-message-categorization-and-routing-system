"""Structured (JSON) logging with automatic request-ID correlation."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

_HANDLER_NAME = "ai-router"

# Request ID of the HTTP request being handled; set by RequestContextMiddleware.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord - everything else passed via `extra=` is an event field.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
    | {"message", "asctime", "request_id", "taskName", "color_message"}  # uvicorn adds ANSI copy
)


def current_request_id() -> str | None:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Attach the current request ID to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def _event_fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, event, request_id + extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        payload.update(_event_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly single-line format for local debugging."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        request_id = getattr(record, "request_id", None)
        rid = f" [{request_id[:8]}]" if request_id else ""
        fields = " ".join(f"{k}={v}" for k, v in _event_fields(record).items())
        line = (
            f"{timestamp} {record.levelname:<7} {record.name}{rid} {record.getMessage()} {fields}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line.rstrip()


def configure_logging(level: str = "INFO", fmt: Literal["json", "console"] = "json") -> None:
    """Configure the root logger and route uvicorn's loggers through it."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(RequestIdFilter())
    handler.set_name(_HANDLER_NAME)

    root = logging.getLogger()
    # Replace only our own handler (idempotent; keeps e.g. pytest's log capture handler).
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME] + [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    # Our middleware logs every request (`http.request`), so uvicorn's access log is redundant.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Third-party HTTP clients are chatty at INFO level.
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
