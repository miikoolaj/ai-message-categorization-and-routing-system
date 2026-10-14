"""Sending e-mails over SMTP (async) plus an in-memory implementation for tests and evaluation."""

import asyncio
import logging
from collections.abc import Sequence
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

import aiosmtplib

from ai_router.logging_config import log_event

if TYPE_CHECKING:
    from ai_router.config import Settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """The message could not be delivered to the SMTP server (after retries)."""


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class SmtpEmailSender:
    """Delivers messages with aiosmtplib, retrying transient failures with a short backoff."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout: float,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = False,
        start_tls: bool = False,
        retry_delays: Sequence[float] = (0.5, 1.5),
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._start_tls = start_tls
        self._retry_delays = tuple(retry_delays)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SmtpEmailSender":
        password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
            username=settings.smtp_username,
            password=password,
            use_tls=settings.smtp_use_tls,
            start_tls=settings.smtp_starttls,
        )

    async def send(self, message: EmailMessage) -> None:
        attempts = len(self._retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                await aiosmtplib.send(
                    message,
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    use_tls=self._use_tls,
                    # Explicit bool: `None` would mean "upgrade automatically if offered".
                    start_tls=self._start_tls,
                    timeout=self._timeout,
                )
            except aiosmtplib.SMTPResponseException as exc:
                if not (400 <= exc.code < 500) or attempt == attempts:
                    raise self._failure(message, exc) from exc
                await self._before_retry(message, attempt, exc)
            except (
                aiosmtplib.SMTPServerDisconnected,
                aiosmtplib.SMTPConnectError,
                aiosmtplib.SMTPTimeoutError,
                TimeoutError,
                OSError,
            ) as exc:
                if attempt == attempts:
                    raise self._failure(message, exc) from exc
                await self._before_retry(message, attempt, exc)
            except aiosmtplib.SMTPException as exc:  # permanent (e.g. recipients refused)
                raise self._failure(message, exc) from exc
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "smtp.sent",
                    message_id=message["Message-ID"],
                    to=message["To"],
                )
                return

    async def _before_retry(self, message: EmailMessage, attempt: int, exc: Exception) -> None:
        delay = self._retry_delays[attempt - 1]
        log_event(
            logger,
            logging.WARNING,
            "smtp.retry",
            message_id=message["Message-ID"],
            attempt=attempt,
            error=repr(exc),
        )
        await asyncio.sleep(delay)

    def _failure(self, message: EmailMessage, exc: Exception) -> EmailDeliveryError:
        log_event(
            logger, logging.ERROR, "smtp.failed", message_id=message["Message-ID"], error=repr(exc)
        )
        return EmailDeliveryError(f"SMTP delivery to {self._host}:{self._port} failed: {exc!r}")


class InMemoryEmailSender:
    """Keeps messages in a list instead of sending them (tests, evaluation)."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with = fail_with

    async def send(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
