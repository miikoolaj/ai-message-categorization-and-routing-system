import json
import logging

from ai_router.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    RequestIdFilter,
    request_id_var,
)


def record(**extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("ai_router.test", logging.INFO, __file__, 1, "email.sent", None, None)
    for key, value in extra.items():
        setattr(rec, key, value)
    RequestIdFilter().filter(rec)
    return rec


def test_json_formatter_includes_request_id_and_event_fields() -> None:
    token = request_id_var.set("req-42")
    try:
        payload = json.loads(
            JsonFormatter().format(record(message_id="<1@x>", to="it@example.com"))
        )
    finally:
        request_id_var.reset(token)
    assert payload["event"] == "email.sent"
    assert payload["request_id"] == "req-42"
    assert payload["message_id"] == "<1@x>"
    assert payload["level"] == "INFO"


def test_console_formatter_is_single_line() -> None:
    line = ConsoleFormatter().format(record(department="it@example.com"))
    assert "email.sent" in line
    assert "department=it@example.com" in line
    assert "\n" not in line
