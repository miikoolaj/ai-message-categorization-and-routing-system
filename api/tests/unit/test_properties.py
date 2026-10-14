"""Property-based tests (Hypothesis): invariants that must hold for ANY input."""

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from ai_router.mail.composer import sanitize_header
from ai_router.web.schemas import RouteMessageRequest
from tests.conftest import POLICY


@given(st.text(), st.integers(min_value=10, max_value=300))
def test_sanitized_header_is_single_line_and_bounded(value: str, max_length: int) -> None:
    result = sanitize_header(value, max_length=max_length)
    assert "\r" not in result and "\n" not in result
    assert len(result) <= max_length
    assert result == result.strip()


@given(st.text())
def test_normalize_department_is_idempotent(value: str) -> None:
    once = POLICY.normalize(value)
    assert POLICY.normalize(once) == once


@settings(max_examples=200)
@given(st.text(max_size=5000))
def test_message_validator_never_returns_control_characters(message: str) -> None:
    try:
        cleaned = RouteMessageRequest(email="jan@example.com", message=message).message
    except ValidationError:
        return
    assert cleaned and cleaned == cleaned.strip()
    assert not any(ord(ch) < 32 and ch not in "\t\n" for ch in cleaned)
    assert "\x7f" not in cleaned and len(cleaned) <= 4000
