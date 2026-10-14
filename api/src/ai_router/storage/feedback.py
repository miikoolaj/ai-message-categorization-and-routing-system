"""Feedback from departments ("wrong department?") - a source of real, labelled data."""

import time
from dataclasses import dataclass

from ai_router.storage.db import Database


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    ticket_id: str
    message: str
    routed_department: str
    correct_department: str
    comment: str | None
    created_at: float


class FeedbackRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        ticket_id: str,
        message: str,
        routed_department: str,
        correct_department: str,
        comment: str | None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO feedback (ticket_id, message, routed_department, correct_department, "
            "comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, message, routed_department, correct_department, comment, time.time()),
        )

    async def all(self) -> list[FeedbackRecord]:
        rows = await self._db.fetchall("SELECT * FROM feedback ORDER BY id")
        return [
            FeedbackRecord(
                ticket_id=row["ticket_id"],
                message=row["message"],
                routed_department=row["routed_department"],
                correct_department=row["correct_department"],
                comment=row["comment"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def purge(self, older_than: float) -> int:
        return await self._db.execute("DELETE FROM feedback WHERE created_at < ?", (older_than,))
