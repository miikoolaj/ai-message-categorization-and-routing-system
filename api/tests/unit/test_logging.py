import json
import logging

from ai_router.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    RequestIdFilter,
    log_event,
    request_id_var,
)


def capture(event: str, **fields: object) -> logging.LogRecord:
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("ai_router.test.capture")
    logger.setLevel(logging.DEBUG)
    handler = Collect()
    handler.addFilter(RequestIdFilter())
    logger.addHandler(handler)
    try:
        log_event(logger, logging.INFO, event, **fields)
    finally:
        logger.removeHandler(handler)
    return records[0]


def test_log_event_accepts_field_names_that_collide_with_logrecord() -> None:
    record = capture("routing.started", message="treść", name="x", args="y", msg="z")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "routing.started"
    assert payload["message"] == "treść"
    assert payload["name"] == "x"


def test_json_formatter_includes_request_id() -> None:
    token = request_id_var.set("req-42")
    try:
        payload = json.loads(JsonFormatter().format(capture("email.sent", to="it@example.com")))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-42"
    assert payload["to"] == "it@example.com"


def test_console_formatter_is_single_line() -> None:
    line = ConsoleFormatter().format(capture("email.sent", department="it@example.com"))
    assert "email.sent" in line
    assert "department=it@example.com" in line
    assert "\n" not in line
