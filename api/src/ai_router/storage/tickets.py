"""Tickets: one row per accepted request (sync or async) - status, result and audit data."""

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Literal

import aiosqlite

from ai_router.storage.db import Database

TicketStatus = Literal["queued", "processing", "routed", "failed"]
TicketMode = Literal["sync", "async"]
STATUSES: tuple[TicketStatus, ...] = ("queued", "processing", "routed", "failed")


@dataclass(frozen=True, slots=True)
class TicketRecord:
    ticket_id: str
    ticket_code: str
    request_id: str
    client_id: str
    mode: TicketMode
    status: TicketStatus
    sender_email: str
    sender_verification: str
    message: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    created_at: float
    updated_at: float


def _record(row: aiosqlite.Row) -> TicketRecord:
    return TicketRecord(
        ticket_id=row["ticket_id"],
        ticket_code=row["ticket_code"],
        request_id=row["request_id"],
        client_id=row["client_id"],
        mode=row["mode"],
        status=row["status"],
        sender_email=row["sender_email"],
        sender_verification=row["sender_verification"],
        message=row["message"],
        result=json.loads(row["result"]) if row["result"] else None,
        error=json.loads(row["error"]) if row["error"] else None,
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TicketRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        ticket_id: str,
        ticket_code: str,
        request_id: str,
        client_id: str,
        mode: TicketMode,
        status: TicketStatus,
        sender_email: str,
        sender_verification: str,
        message: str,
    ) -> None:
        now = time.time()
        await self._db.execute(
            "INSERT INTO tickets (ticket_id, ticket_code, request_id, client_id, mode, status, "
            "sender_email, sender_verification, message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                ticket_code,
                request_id,
                client_id,
                mode,
                status,
                sender_email,
                sender_verification,
                message,
                now,
                now,
            ),
        )

    async def get(self, ticket_id: str) -> TicketRecord | None:
        row = await self._db.fetchone("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
        return _record(row) if row else None

    async def complete(
        self,
        ticket_id: str,
        *,
        result: dict[str, Any],
        department: str,
        routed_by: str,
        prompt_version: str,
    ) -> None:
        await self._db.execute(
            "UPDATE tickets SET status = 'routed', result = ?, error = NULL, department = ?, "
            "routed_by = ?, prompt_version = ?, updated_at = ? WHERE ticket_id = ?",
            (json.dumps(result), department, routed_by, prompt_version, time.time(), ticket_id),
        )

    async def fail(self, ticket_id: str, *, error: dict[str, Any]) -> None:
        await self._db.execute(
            "UPDATE tickets SET status = 'failed', error = ?, updated_at = ? WHERE ticket_id = ?",
            (json.dumps(error), time.time(), ticket_id),
        )

    async def claim_next_queued(self) -> TicketRecord | None:
        """Atomically move the oldest queued ticket to `processing` (async worker)."""
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM tickets WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE tickets SET status = 'processing', attempts = attempts + 1, "
                "updated_at = ? WHERE ticket_id = ?",
                (time.time(), row["ticket_id"]),
            )
        ticket = _record(row)
        return replace(ticket, status="processing", attempts=ticket.attempts + 1)

    async def recover_interrupted(self) -> tuple[int, int]:
        """After a restart: async tickets go back to the queue, sync ones are marked failed."""
        requeued = await self._db.execute(
            "UPDATE tickets SET status = 'queued', updated_at = ? "
            "WHERE status = 'processing' AND mode = 'async'",
            (time.time(),),
        )
        failed = await self._db.execute(
            "UPDATE tickets SET status = 'failed', error = ?, updated_at = ? "
            "WHERE status = 'processing' AND mode = 'sync'",
            (json.dumps({"type": "interrupted", "title": "Service restarted"}), time.time()),
        )
        return requeued, failed

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self._db.fetchall("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")
        counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
        counts.update({row["status"]: row["n"] for row in rows})
        return counts

    async def purge(self, older_than: float) -> int:
        """Retention: delete finished tickets (and their message text) older than the cut-off."""
        return await self._db.execute(
            "DELETE FROM tickets WHERE status IN ('routed', 'failed') AND updated_at < ?",
            (older_than,),
        )
