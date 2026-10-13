from datetime import UTC, datetime
from email import message_from_bytes, policy

from ai_router.mail.composer import MailSettings, compose_routed_email, sanitize_header
from ai_router.routing.departments import Department

MAIL = MailSettings(
    from_address="AI Router <ai-router@example.com>",
    subject_prefix="[AI Router]",
    message_id_domain="ai-router.local",
)


def compose(**overrides: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "sender_email": "jan.nowak@example.com",
        "department": Department.HELP_DESK,
        "subject": "Komputer nie działa",
        "reason": "Awaria sprzętu użytkownika",
        "original_message": "Nie działa mi komputer",
        "request_id": "req-123",
        "routed_by": "agent",
        "model_name": "qwen3:4b",
        "mail_settings": MAIL,
        "now": datetime(2026, 10, 13, 10, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return compose_routed_email(**kwargs)  # type: ignore[arg-type]


def roundtrip(message):  # type: ignore[no-untyped-def]
    return message_from_bytes(message.as_bytes(), policy=policy.default)


def test_core_headers() -> None:
    msg = roundtrip(compose())
    assert msg["To"] == "help-desk@example.com"
    assert msg["Reply-To"] == "jan.nowak@example.com"
    assert msg["From"] == "AI Router <ai-router@example.com>"
    assert msg["Subject"] == "[AI Router] Komputer nie działa"
    assert msg["X-Request-ID"] == "req-123"
    assert msg["X-AI-Router-Department"] == "help-desk@example.com"
    assert msg["X-AI-Router-Routed-By"] == "agent"
    assert msg["X-AI-Router-Model"] == "qwen3:4b"
    assert msg["Date"] is not None


def test_message_id_is_unique_and_uses_configured_domain() -> None:
    first, second = compose()["Message-ID"], compose()["Message-ID"]
    assert first != second
    assert first.endswith("@ai-router.local>")


def test_body_contains_original_message_verbatim_and_metadata() -> None:
    original = "Linia 1 <script>alert(1)</script>\nLinia 2: zażółć gęślą jaźń"
    body = roundtrip(compose(original_message=original)).get_content()
    assert original in body
    assert "jan.nowak@example.com" in body
    assert "Awaria sprzętu użytkownika" in body
    assert "req-123" in body


def test_polish_characters_survive_encoding() -> None:
    msg = roundtrip(compose(subject="Zażółć gęślą jaźń"))
    assert msg["Subject"] == "[AI Router] Zażółć gęślą jaźń"


def test_header_injection_attempt_is_neutralised() -> None:
    msg = roundtrip(compose(subject="Hello\r\nBcc: evil@example.com"))
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"]
    assert msg["Subject"] == "[AI Router] Hello Bcc: evil@example.com"


def test_fallback_mode_is_marked() -> None:
    msg = roundtrip(compose(routed_by="fallback", department=Department.OTHER))
    assert msg["X-AI-Router-Routed-By"] == "fallback"
    assert "fallback" in msg.get_content()


def test_sanitize_header_collapses_whitespace_and_truncates_at_word_boundary() -> None:
    assert sanitize_header("  a\t\tb \n c  ") == "a b c"
    text = "słowo " * 40
    result = sanitize_header(text, max_length=50)
    assert len(result) <= 50
    assert result.endswith("słowo")
