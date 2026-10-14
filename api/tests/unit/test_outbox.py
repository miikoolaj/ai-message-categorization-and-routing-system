"""Outbox dispatcher and gateway: durability, retries, no double delivery."""

import asyncio
import time
from collections.abc import AsyncIterator
from email.message import EmailMessage

import pytest

from ai_router.mail.gateway import MailGatewayError
from ai_router.mail.outbox import OutboxDispatcher, OutboxGateway
from ai_router.mail.sender import EmailDeliveryError, InMemoryEmailSender
from ai_router.storage.db import Database
from ai_router.storage.outbox import OutboxRepository
from tests.unit.test_storage import record

pytestmark = pytest.mark.anyio


class FlakySender:
    def __init__(self, failures: int = 0, delay: float = 0.0) -> None:
        self.failures = failures
        self.delay = delay
        self.sent: list[EmailMessage] = []
        self.calls = 0

    async def send(self, message: EmailMessage) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures > 0:
            self.failures -= 1
            raise EmailDeliveryError("smtp down")
        self.sent.append(message)


@pytest.fixture
async def repo() -> AsyncIterator[OutboxRepository]:
    db = Database(":memory:")
    await db.connect()
    yield OutboxRepository(db)
    await db.close()


def email(message_id: str = "<1@x>") -> EmailMessage:
    msg = EmailMessage()
    msg["To"], msg["Subject"], msg["Message-ID"] = "it@example.com", "Test", message_id
    msg.set_content("x")
    return msg


def dispatcher(repo: OutboxRepository, sender: object, max_attempts: int = 3) -> OutboxDispatcher:
    return OutboxDispatcher(
        repo,
        sender,
        max_attempts=max_attempts,
        retry_base_seconds=0.01,
        retry_max_seconds=0.05,  # type: ignore[arg-type]
        poll_interval=0.01,
    )


async def test_gateway_delivers_immediately(repo: OutboxRepository) -> None:
    sender = InMemoryEmailSender()
    gateway = OutboxGateway(repo, dispatcher(repo, sender), sync_timeout=1)
    assert await gateway.submit(email(), record()) == "sent"
    assert [m["Message-ID"] for m in sender.sent] == ["<1@x>"]
    found = await gateway.find("t1")
    assert found is not None and found.delivery == "sent"


async def test_smtp_outage_queues_and_background_loop_retries(repo: OutboxRepository) -> None:
    sender = FlakySender(failures=2)
    disp = dispatcher(repo, sender, max_attempts=5)
    gateway = OutboxGateway(repo, disp, sync_timeout=1)
    assert await gateway.submit(email(), record()) == "queued"  # nothing lost
    loop = asyncio.create_task(disp.run())
    try:
        for _ in range(200):
            if sender.sent:
                break
            await asyncio.sleep(0.02)
    finally:
        loop.cancel()
    assert len(sender.sent) == 1 and sender.calls == 3
    found = await gateway.find("t1")
    assert found is not None and found.delivery == "sent"


async def test_entry_fails_after_max_attempts(repo: OutboxRepository) -> None:
    sender = FlakySender(failures=10)
    disp = dispatcher(repo, sender, max_attempts=2)
    entry_id = await repo.enqueue(record(), email().as_bytes(), first_attempt_at=time.time())
    assert await disp.deliver(entry_id) is False
    entry = await repo.claim(entry_id)  # rescheduled -> pending again
    assert entry is not None
    await repo.reschedule(entry_id, next_attempt_at=time.time(), error="x")
    assert await disp.deliver(entry_id) is False
    assert await repo.pending_count() == 0  # marked failed, not retried forever


async def test_slow_smtp_is_not_cut_and_never_duplicated(repo: OutboxRepository) -> None:
    sender = FlakySender(delay=0.3)
    disp = dispatcher(repo, sender)
    gateway = OutboxGateway(repo, disp, sync_timeout=0.05)
    assert await gateway.submit(email(), record()) == "queued"  # caller stopped waiting...
    await asyncio.sleep(0.5)
    assert len(sender.sent) == 1  # ...but the SMTP transaction finished and was recorded
    found = await gateway.find("t1")
    assert found is not None and found.delivery == "sent"
    assert await disp.deliver(1) is False  # already sent - no second delivery


async def test_concurrent_deliver_calls_share_one_attempt(repo: OutboxRepository) -> None:
    sender = FlakySender(delay=0.05)
    disp = dispatcher(repo, sender)
    entry_id = await repo.enqueue(record(), email().as_bytes(), first_attempt_at=time.time())
    results = await asyncio.gather(*(disp.deliver(entry_id) for _ in range(5)))
    assert sender.calls == 1 and results.count(True) == 5


async def test_storage_failure_surfaces_as_gateway_error() -> None:
    db = Database(":memory:")  # never connected
    gateway = OutboxGateway(
        OutboxRepository(db),
        dispatcher(OutboxRepository(db), InMemoryEmailSender()),
        sync_timeout=1,
    )
    with pytest.raises(MailGatewayError):
        await gateway.submit(email(), record())
    with pytest.raises(MailGatewayError):
        await gateway.find("t1")
