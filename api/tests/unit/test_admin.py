"""Operator CLI (`python -m ai_router.admin`) on a real SQLite file."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from ai_router import admin
from ai_router.storage.db import Database
from ai_router.storage.feedback import FeedbackRepository
from ai_router.storage.idempotency import IdempotencyRepository
from ai_router.storage.outbox import OutboxRepository
from ai_router.storage.tickets import TicketRepository
from tests.unit.test_storage import record

pytestmark = pytest.mark.anyio


async def seed(path: Path) -> None:
    db = Database(str(path))
    await db.connect()
    tickets, outbox = TicketRepository(db), OutboxRepository(db)
    for ticket_id, sender in (("a" * 32, "Jan@Example.com"), ("b" * 32, "ewa@example.com")):
        await tickets.create(
            ticket_id=ticket_id,
            ticket_code=f"AIR-{ticket_id[:8].upper()}",
            request_id="req",
            client_id="anonymous",
            mode="sync",
            status="processing",
            sender_email=sender,
            sender_verification="none",
            message="Nie działa mi komputer",
        )
        entry = await outbox.enqueue(
            record(ticket_id, f"<{ticket_id}@x>"), b"raw", first_attempt_at=time.time()
        )
        await outbox.mark_failed(entry, error="SMTP down")
    await FeedbackRepository(db).add(
        ticket_id="a" * 32,
        message="Nie działa mi komputer",
        routed_department="it@example.com",
        correct_department="help-desk@example.com",
        comment=None,
    )
    replays = IdempotencyRepository(db, ttl_seconds=60)
    await replays.begin("anonymous:k1", "fp")
    await replays.complete(
        "anonymous:k1", status_code=200, body={"email": {"reply_to": "jan@example.com"}}, headers={}
    )
    await db.close()


def run(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, object]:
    assert admin.main(list(args)) == 0
    result: dict[str, object] = json.loads(capsys.readouterr().out)
    return result


async def test_stats_retry_and_forget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "app.sqlite3"
    await seed(path)
    db = ["--db", str(path)]

    stats = run(capsys, *db, "stats")
    assert stats["tickets"] == {"processing": 2}
    assert stats["outbox"] == {"failed": 2}
    assert stats["feedback"] == 1 and stats["idempotency_keys"] == 1

    assert run(capsys, *db, "retry-failed") == {"requeued": 2}
    assert run(capsys, *db, "stats")["outbox"] == {"pending": 2}

    forgotten = run(capsys, *db, "forget", " JAN@example.com ")
    assert forgotten["removed"] == {"feedback": 1, "outbox": 1, "tickets": 1, "idempotency": 1}
    with closing(sqlite3.connect(path)) as conn:
        senders = [row[0] for row in conn.execute("SELECT sender_email FROM tickets")]
    assert senders == ["ewa@example.com"]  # other people's data untouched


async def test_backup_is_a_consistent_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "app.sqlite3"
    await seed(path)
    target = tmp_path / "backup.sqlite3"
    result = run(capsys, "--db", str(path), "backup", str(target))
    assert result["backup"] == str(target)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        admin.main(["--db", str(path), "backup", str(target)])


def test_missing_database_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="database not found"):
        admin.main(["--db", str(tmp_path / "nope.sqlite3"), "stats"])
