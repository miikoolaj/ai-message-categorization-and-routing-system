"""SMTP delivery against the real Mailpit container (run: make test-integration)."""

import uuid

import httpx
import pytest

from ai_router.config import Settings
from ai_router.mail.composer import MailSettings, compose_routed_email
from ai_router.mail.sender import SmtpEmailSender
from ai_router.routing.departments import Department
from tests.helpers.mailpit import MAILPIT_BASE_URL, find_message, message_headers

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_smtp_sender_delivers_with_reply_to() -> None:
    settings = Settings()
    marker = uuid.uuid4().hex[:12]
    sender_address = f"integration-{marker}@example.com"
    email = compose_routed_email(
        sender_email=sender_address,
        department=Department.KADRY,
        subject="Test integracyjny - urlop",
        reason="Wniosek urlopowy",
        original_message="Chciałbym zgłosić urlop na jutro (zażółć gęślą jaźń)",
        request_id=f"it-{marker}",
        routed_by="agent",
        model_name="integration-test",
        mail_settings=MailSettings.from_settings(settings),
    )
    await SmtpEmailSender.from_settings(settings).send(email)

    async with httpx.AsyncClient(base_url=MAILPIT_BASE_URL, timeout=10) as mailpit:
        captured = await find_message(mailpit, f'reply-to:"{sender_address}"')
        headers = await message_headers(mailpit, captured["ID"])

    assert captured["To"][0]["Address"] == "kadry@example.com"
    assert captured["ReplyTo"][0]["Address"] == sender_address
    assert captured["Subject"] == "[AI Router] Test integracyjny - urlop"
    assert "zażółć gęślą jaźń" in captured["Text"]
    assert headers["x-request-id"] == [f"it-{marker}"]
