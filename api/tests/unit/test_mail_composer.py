from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any

import pytest

from ai_router.mail.composer import (
    AI_NOTE,
    VERIFICATION_BANNERS,
    MailSettings,
    compose_routed_email,
    sanitize_header,
)

MAIL = MailSettings(
    from_address="AI Router <ai-router@example.com>",
    subject_prefix="[AI Router]",
    message_id_domain="ai-router.local",
)


def compose(**overrides: Any) -> EmailMessage:
    kwargs: dict[str, Any] = {
        "sender_email": "jan.nowak@example.com",
        "department": "help-desk@example.com",
        "subject": "Komputer nie działa",
        "reason": "Awaria sprzętu użytkownika",
        "original_message": "Nie działa mi komputer",
        "request_id": "req-123",
        "ticket_code": "AIR-ABCDEF12",
        "routed_by": "agent",
        "model_name": "qwen3:4b",
        "prompt_version": "1a2b3c4d",
        "mail_settings": MAIL,
        "confidence": 0.87,
        "now": datetime(2026, 10, 13, 10, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return compose_routed_email(**kwargs)


def roundtrip(message: EmailMessage) -> EmailMessage:
    parsed = message_from_bytes(message.as_bytes(), policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def text_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return str(part.get_content())


def html_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("html",))
    assert part is not None
    return str(part.get_content())


def test_core_headers() -> None:
    msg = roundtrip(compose())
    assert msg["To"] == "help-desk@example.com"
    assert msg["Reply-To"] == "jan.nowak@example.com"
    assert msg["From"] == "AI Router <ai-router@example.com>"
    assert msg["Subject"] == "[AI Router] [AIR-ABCDEF12] Komputer nie działa"
    assert msg["X-Request-ID"] == "req-123"
    assert msg["X-AI-Router-Ticket"] == "AIR-ABCDEF12"
    assert msg["X-AI-Router-Prompt-Version"] == "1a2b3c4d"
    assert msg["X-AI-Router-Confidence"] == "0.87"
    assert msg["X-AI-Router-Needs-Review"] == "false"
    assert msg["X-AI-Router-Sender-Verification"] == "none"


def test_plain_text_and_html_alternatives_contain_the_original_verbatim() -> None:
    original = "Linia 1 <script>alert(1)</script>\nLinia 2: zażółć gęślą jaźń"
    msg = roundtrip(compose(original_message=original))
    assert msg.get_content_type() == "multipart/alternative"
    assert original in text_of(msg)
    html = html_of(msg)
    assert "<script>" not in html  # escaped in HTML
    assert "&lt;script&gt;" in html
    assert AI_NOTE in text_of(msg)


@pytest.mark.parametrize("verification", ["none", "client", "sso"])
def test_sender_verification_banner(verification: str) -> None:
    msg = roundtrip(compose(sender_verification=verification))
    assert VERIFICATION_BANNERS[verification] in text_of(msg)  # type: ignore[index]
    assert msg["X-AI-Router-Sender-Verification"] == verification


def test_needs_review_marks_subject_and_body() -> None:
    msg = roundtrip(compose(confidence=0.31, needs_review=True))
    assert "[Do weryfikacji]" in msg["Subject"]
    assert msg["X-AI-Router-Needs-Review"] == "true"
    assert "poniżej progu" in text_of(msg)


def test_feedback_link_is_included() -> None:
    url = "http://test/api/v1/feedback/abc?token=xyz"
    msg = roundtrip(compose(feedback_url=url))
    assert url in text_of(msg)
    assert "Zły dział?" in html_of(msg)


def test_message_id_unique_with_configured_domain() -> None:
    first, second = compose()["Message-ID"], compose()["Message-ID"]
    assert first != second
    assert first.endswith("@ai-router.local>")


def test_header_injection_attempt_is_neutralised() -> None:
    msg = roundtrip(compose(subject="Hello\r\nBcc: evil@example.com"))
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"]


def test_fallback_mode_is_marked() -> None:
    msg = roundtrip(compose(routed_by="fallback", department="other@example.com", confidence=None))
    assert msg["X-AI-Router-Routed-By"] == "fallback"
    assert msg["X-AI-Router-Confidence"] is None
    assert "fallback" in text_of(msg)


def test_sanitize_header_collapses_whitespace_and_truncates_at_word_boundary() -> None:
    assert sanitize_header("  a\t\tb \n c  ") == "a b c"
    result = sanitize_header("słowo " * 40, max_length=50)
    assert len(result) <= 50
    assert result.endswith("słowo")
