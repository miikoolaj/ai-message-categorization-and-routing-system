from email.message import EmailMessage
from typing import Any

import aiosmtplib
import pytest

from ai_router.mail.sender import EmailDeliveryError, InMemoryEmailSender, SmtpEmailSender

pytestmark = pytest.mark.anyio


def message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@example.com", "b@example.com", "Test"
    msg["Message-ID"] = "<1@test>"
    msg.set_content("x")
    return msg


def sender() -> SmtpEmailSender:
    return SmtpEmailSender(host="smtp", port=25, timeout=1, retry_delays=(0, 0))


def fake_send(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def send(msg: EmailMessage, **kwargs: Any) -> None:
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(aiosmtplib, "send", send)
    return calls


async def test_success_passes_explicit_tls_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_send(monkeypatch, [None])
    await sender().send(message())
    assert len(calls) == 1
    assert calls[0]["start_tls"] is False  # never "auto"
    assert calls[0]["use_tls"] is False
    assert calls[0]["hostname"] == "smtp"


async def test_transient_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_send(monkeypatch, [aiosmtplib.SMTPServerDisconnected("gone"), None])
    await sender().send(message())
    assert len(calls) == 2


async def test_temporary_4xx_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_send(monkeypatch, [aiosmtplib.SMTPResponseException(451, "try later"), None])
    await sender().send(message())
    assert len(calls) == 2


async def test_permanent_5xx_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_send(monkeypatch, [aiosmtplib.SMTPResponseException(550, "no such user")])
    with pytest.raises(EmailDeliveryError):
        await sender().send(message())
    assert len(calls) == 1


async def test_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[Exception | None] = [ConnectionRefusedError("refused")] * 3
    calls = fake_send(monkeypatch, errors)
    with pytest.raises(EmailDeliveryError):
        await sender().send(message())
    assert len(calls) == 3


async def test_in_memory_sender_records_and_can_fail() -> None:
    mem = InMemoryEmailSender()
    await mem.send(message())
    assert len(mem.sent) == 1
    failing = InMemoryEmailSender(fail_with=EmailDeliveryError("down"))
    with pytest.raises(EmailDeliveryError):
        await failing.send(message())
