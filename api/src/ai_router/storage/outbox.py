"""Outbox table: e-mails accepted for delivery and their delivery state."""

import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ai_router.routing.deps import SentEmail
from ai_router.storage.db import Database

OutboxStatus = Literal["pending", "sending", "sent", "failed"]


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    ticket_id: str
    message_id: str
    record: dict[str, Any]
    raw: bytes
    status: OutboxStatus
    attempts: int


class OutboxRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(self, record: SentEmail, raw: bytes, *, first_attempt_at: float) -> int:
        payload = {k: v for k, v in asdict(record).items() if k != "delivery"}
        return await self._db.insert(
            "INSERT INTO outbox (ticket_id, message_id, record, raw, status, next_attempt_at, "
            "created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (
                record.ticket_id,
                record.message_id,
                json.dumps(payload),
                raw,
                first_attempt_at,
                time.time(),
            ),
        )

    async def claim(self, entry_id: int) -> OutboxEntry | None:
        """Atomically take a pending entry for delivery (`pending` -> `sending`)."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE outbox SET status = 'sending' WHERE id = ? AND status = 'pending'",
                (entry_id,),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            if not claimed:
                return None
            async with conn.execute("SELECT * FROM outbox WHERE id = ?", (entry_id,)) as cur:
                row = await cur.fetchone()
        if row is None:  # pragma: no cover - deleted concurrently
            return None
        return OutboxEntry(
            id=row["id"],
            ticket_id=row["ticket_id"],
            message_id=row["message_id"],
            record=json.loads(row["record"]),
            raw=bytes(row["raw"]),
            status="sending",
            attempts=row["attempts"],
        )

    async def mark_sent(self, entry_id: int) -> None:
        await self._db.execute(
            "UPDATE outbox SET status = 'sent', attempts = attempts + 1, sent_at = ?, "
            "last_error = NULL WHERE id = ?",
            (time.time(), entry_id),
        )

    async def reschedule(self, entry_id: int, *, next_attempt_at: float, error: str) -> None:
        await self._db.execute(
            "UPDATE outbox SET status = 'pending', attempts = attempts + 1, next_attempt_at = ?, "
            "last_error = ? WHERE id = ?",
            (next_attempt_at, error[:500], entry_id),
        )

    async def mark_failed(self, entry_id: int, *, error: str) -> None:
        await self._db.execute(
            "UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ? "
            "WHERE id = ?",
            (error[:500], entry_id),
        )

    async def due(self, *, now: float, limit: int = 20) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT id FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at LIMIT ?",
            (now, limit),
        )
        return [int(row["id"]) for row in rows]

    async def find_by_ticket(self, ticket_id: str) -> tuple[dict[str, Any], OutboxStatus] | None:
        row = await self._db.fetchone(
            "SELECT record, status FROM outbox WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        )
        return (json.loads(row["record"]), row["status"]) if row else None

    async def reset_stuck(self) -> int:
        """After a crash: entries left in `sending` are retried (at-least-once delivery)."""
        return await self._db.execute(
            "UPDATE outbox SET status = 'pending', next_attempt_at = ? WHERE status = 'sending'",
            (time.time(),),
        )

    async def pending_count(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM outbox WHERE status IN ('pending', 'sending')"
        )
        return int(row["n"]) if row else 0

    async def purge(self, older_than: float) -> int:
        return await self._db.execute(
            "DELETE FROM outbox WHERE status IN ('sent', 'failed') AND created_at < ?",
            (older_than,),
        )
