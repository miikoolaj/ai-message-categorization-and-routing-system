"""Transactional outbox: durable acceptance of e-mails and background delivery with retries.

Flow for one message:
1. `OutboxGateway.submit()` stores the serialized e-mail in SQLite (`pending`) - from now on the
   message cannot be lost, even if SMTP is down or the process restarts.
2. It immediately asks the dispatcher to deliver it and waits up to
   `MAIL_SYNC_DELIVERY_TIMEOUT_SECONDS`. Delivered -> `sent`; otherwise -> `queued`.
3. The delivery attempt runs as its own task: if the caller stops waiting (timeout), the SMTP
   transaction still finishes and the outcome is recorded - it is never cut in half.
4. Failed attempts are rescheduled with exponential backoff and jitter; after
   `OUTBOX_MAX_ATTEMPTS` the entry is marked `failed` (alert-worthy, see metrics).
"""

import asyncio
import contextlib
import email
import logging
import random
import time
from email import policy as email_policy
from email.message import EmailMessage

from ai_router.logging_config import log_event
from ai_router.mail.gateway import MailGatewayError
from ai_router.mail.sender import EmailDeliveryError, EmailSender
from ai_router.observability import metrics
from ai_router.routing.deps import DeliveryStatus, SentEmail
from ai_router.storage.db import StorageError
from ai_router.storage.outbox import OutboxRepository

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(
        self,
        repo: OutboxRepository,
        sender: EmailSender,
        *,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        poll_interval: float = 1.0,
    ) -> None:
        self._repo = repo
        self._sender = sender
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._inflight: dict[int, asyncio.Task[bool]] = {}

    def notify(self) -> None:
        self._wake.set()

    def _task_for(self, entry_id: int) -> asyncio.Task[bool]:
        """One delivery task per entry, shared by concurrent callers."""
        task = self._inflight.get(entry_id)
        if task is None:
            task = asyncio.create_task(self._attempt(entry_id), name=f"outbox-{entry_id}")
            self._inflight[entry_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(entry_id, None))
        return task

    async def deliver(self, entry_id: int, *, wait_seconds: float | None = None) -> bool:
        """Deliver one entry now. Returns True if sent within `wait_seconds` (the attempt
        continues in the background when the caller stops waiting)."""
        task = self._task_for(entry_id)
        done, _ = await asyncio.wait({task}, timeout=wait_seconds)
        if task not in done or task.cancelled() or task.exception() is not None:
            return False
        return task.result()

    def _backoff(self, attempts: int) -> float:
        delay = min(self._retry_base * 2.0 ** max(attempts - 1, 0), self._retry_max)
        return delay * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not cryptography

    async def _attempt(self, entry_id: int) -> bool:
        entry = await self._repo.claim(entry_id)
        if entry is None:
            return False  # already sent, failed or being delivered by another task
        message = email.message_from_bytes(entry.raw, policy=email_policy.default)
        try:
            await self._sender.send(message)
        except (EmailDeliveryError, OSError, TimeoutError) as exc:
            attempts = entry.attempts + 1
            if attempts >= self._max_attempts:
                await self._repo.mark_failed(entry_id, error=repr(exc))
                metrics.OUTBOX_DELIVERIES.labels("failed").inc()
                log_event(
                    logger,
                    logging.ERROR,
                    "outbox.failed",
                    message_id=entry.message_id,
                    ticket_id=entry.ticket_id,
                    attempts=attempts,
                    error=repr(exc),
                )
            else:
                delay = self._backoff(attempts)
                await self._repo.reschedule(
                    entry_id, next_attempt_at=time.time() + delay, error=repr(exc)
                )
                metrics.OUTBOX_DELIVERIES.labels("retry").inc()
                log_event(
                    logger,
                    logging.WARNING,
                    "outbox.retry_scheduled",
                    message_id=entry.message_id,
                    attempts=attempts,
                    retry_in_s=round(delay, 1),
                )
            return False
        await self._repo.mark_sent(entry_id)
        metrics.OUTBOX_DELIVERIES.labels("sent").inc()
        log_event(logger, logging.INFO, "outbox.sent", message_id=entry.message_id)
        return True

    async def run(self) -> None:
        """Background loop: deliver due entries (retries and anything left after a restart)."""
        while True:
            try:
                for entry_id in await self._repo.due(now=time.time()):
                    await self.deliver(entry_id)
            except StorageError as exc:
                log_event(logger, logging.ERROR, "outbox.storage_error", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            self._wake.clear()


class OutboxGateway:
    """Production `MailGateway`: accept durably, try to deliver immediately, retry later."""

    def __init__(
        self, repo: OutboxRepository, dispatcher: OutboxDispatcher, *, sync_timeout: float
    ) -> None:
        self._repo = repo
        self._dispatcher = dispatcher
        self._sync_timeout = sync_timeout

    async def submit(self, message: EmailMessage, record: SentEmail) -> DeliveryStatus:
        try:
            # First background attempt only after the immediate one had its chance.
            entry_id = await self._repo.enqueue(
                record, message.as_bytes(), first_attempt_at=time.time() + self._sync_timeout + 1
            )
        except StorageError as exc:
            raise MailGatewayError("could not store the e-mail in the outbox") from exc
        delivered = await self._dispatcher.deliver(entry_id, wait_seconds=self._sync_timeout)
        return "sent" if delivered else "queued"

    async def find(self, ticket_id: str) -> SentEmail | None:
        try:
            found = await self._repo.find_by_ticket(ticket_id)
        except StorageError as exc:
            raise MailGatewayError("could not read the outbox") from exc
        if found is None:
            return None
        payload, status = found
        return SentEmail(**payload, delivery="sent" if status == "sent" else "queued")
