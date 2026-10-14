"""SQLite repositories (in-memory database)."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from ai_router.routing.deps import SentEmail
from ai_router.storage.db import Database, StorageError
from ai_router.storage.feedback import FeedbackRepository
from ai_router.storage.idempotency import IdempotencyRepository
from ai_router.storage.outbox import OutboxRepository
from ai_router.storage.tickets import TicketRepository

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


def record(ticket: str = "t1", message_id: str = "<1@x>") -> SentEmail:
    return SentEmail(
        ticket_id=ticket,
        department="it@example.com",
        chosen_department="it@example.com",
        reply_to="jan@example.com",
        subject="S",
        reason="R",
        message_id=message_id,
        routed_by="agent",
        confidence=0.9,
        needs_review=False,
        delivery="queued",
    )


async def create_ticket(
    repo: TicketRepository, ticket_id: str, *, mode: str = "sync", status: str = "processing"
) -> None:
    await repo.create(
        ticket_id=ticket_id,
        ticket_code=f"AIR-{ticket_id[:8].upper()}",
        request_id="req",
        client_id="anonymous",
        mode=mode,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        sender_email="jan@example.com",
        sender_verification="none",
        message="Nie działa mi komputer",
    )


async def test_migrations_create_schema_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "data" / "app.sqlite3")
    for _ in range(2):  # second connect must not fail on an existing schema
        database = Database(path)
        await database.connect()
        await database.ping()
        await database.close()


async def test_operations_on_a_closed_database_raise_storage_error() -> None:
    with pytest.raises(StorageError):
        await Database(":memory:").ping()


async def test_secret_is_generated_once(db: Database) -> None:
    first = await db.get_or_create_secret("feedback")
    assert first == await db.get_or_create_secret("feedback")
    assert len(first) >= 32


async def test_ticket_lifecycle(db: Database) -> None:
    repo = TicketRepository(db)
    await create_ticket(repo, "a" * 32)
    ticket = await repo.get("a" * 32)
    assert (
        ticket is not None
        and ticket.status == "processing"
        and ticket.ticket_code == "AIR-AAAAAAAA"
    )
    await repo.complete(
        "a" * 32,
        result={"x": 1},
        department="it@example.com",
        routed_by="agent",
        prompt_version="v1",
    )
    done = await repo.get("a" * 32)
    assert done is not None and done.status == "routed" and done.result == {"x": 1}
    await create_ticket(repo, "b" * 32)
    await repo.fail("b" * 32, error={"type": "agent-failed", "title": "T"})
    failed = await repo.get("b" * 32)
    assert failed is not None and failed.error == {"type": "agent-failed", "title": "T"}
    counts = await repo.counts_by_status()
    assert counts["routed"] == 1 and counts["failed"] == 1 and counts["queued"] == 0


async def test_claim_next_queued_is_fifo_and_atomic(db: Database) -> None:
    repo = TicketRepository(db)
    await create_ticket(repo, "1" * 32, mode="async", status="queued")
    await create_ticket(repo, "2" * 32, mode="async", status="queued")
    first = await repo.claim_next_queued()
    second = await repo.claim_next_queued()
    assert first is not None and first.ticket_id == "1" * 32 and first.status == "processing"
    assert second is not None and second.ticket_id == "2" * 32
    assert await repo.claim_next_queued() is None


async def test_recover_interrupted_requeues_async_and_fails_sync(db: Database) -> None:
    repo = TicketRepository(db)
    await create_ticket(repo, "a" * 32, mode="async", status="processing")
    await create_ticket(repo, "b" * 32, mode="sync", status="processing")
    assert await repo.recover_interrupted() == (1, 1)
    assert (await repo.get("a" * 32)).status == "queued"  # type: ignore[union-attr]
    assert (await repo.get("b" * 32)).status == "failed"  # type: ignore[union-attr]


async def test_ticket_retention_purges_only_finished_tickets(db: Database) -> None:
    repo = TicketRepository(db)
    await create_ticket(repo, "a" * 32)
    await repo.complete("a" * 32, result={}, department="x", routed_by="agent", prompt_version="v")
    await create_ticket(repo, "b" * 32, mode="async", status="queued")
    assert await repo.purge(time.time() + 1) == 1
    assert await repo.get("b" * 32) is not None


async def test_outbox_state_machine(db: Database) -> None:
    repo = OutboxRepository(db)
    entry_id = await repo.enqueue(record(), b"raw", first_attempt_at=time.time() - 1)
    assert await repo.due(now=time.time()) == [entry_id]
    claimed = await repo.claim(entry_id)
    assert claimed is not None and claimed.status == "sending" and claimed.raw == b"raw"
    assert await repo.claim(entry_id) is None  # cannot be claimed twice
    await repo.reschedule(entry_id, next_attempt_at=time.time() + 60, error="down")
    assert await repo.due(now=time.time()) == []
    assert await repo.pending_count() == 1
    assert await repo.claim(entry_id) is not None
    await repo.mark_sent(entry_id)
    assert await repo.pending_count() == 0
    found = await repo.find_by_ticket("t1")
    assert found is not None and found[1] == "sent" and found[0]["department"] == "it@example.com"


async def test_outbox_reset_stuck_and_failed(db: Database) -> None:
    repo = OutboxRepository(db)
    entry_id = await repo.enqueue(record(), b"raw", first_attempt_at=time.time())
    await repo.claim(entry_id)
    assert await repo.reset_stuck() == 1  # crash while sending -> retried
    await repo.claim(entry_id)
    await repo.mark_failed(entry_id, error="permanent")
    assert await repo.pending_count() == 0
    assert await repo.purge(time.time() + 1) == 1


async def test_idempotency_states(db: Database) -> None:
    repo = IdempotencyRepository(db, ttl_seconds=60)
    assert (await repo.begin("c:k", "fp1")).kind == "new"
    assert (await repo.begin("c:k", "fp1")).kind == "in_progress"
    assert (await repo.begin("c:k", "fp2")).kind == "conflict"
    await repo.complete("c:k", status_code=200, body={"ok": True}, headers={"X": "1"})
    replay = await repo.begin("c:k", "fp1")
    assert replay.kind == "replay" and replay.body == {"ok": True} and replay.headers == {"X": "1"}
    assert (await repo.begin("other:k", "fp1")).kind == "new"  # scoped per client
    await repo.abandon("other:k")
    assert (await repo.begin("other:k", "fp1")).kind == "new"
    assert await repo.take_over("other:k", "fp1") is True


async def test_idempotency_entries_expire(db: Database) -> None:
    repo = IdempotencyRepository(db, ttl_seconds=0.01)
    await repo.begin("c:k", "fp")
    await asyncio.sleep(0.02)
    assert (await repo.begin("c:k", "fp")).kind == "new"
    await asyncio.sleep(0.02)
    assert await repo.purge_expired() == 1


async def test_feedback_repository(db: Database) -> None:
    repo = FeedbackRepository(db)
    await repo.add(
        ticket_id="t",
        message="m",
        routed_department="it@example.com",
        correct_department="help-desk@example.com",
        comment=None,
    )
    records = await repo.all()
    assert len(records) == 1 and records[0].correct_department == "help-desk@example.com"
    assert await repo.purge(time.time() + 1) == 1
