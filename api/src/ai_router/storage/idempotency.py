"""Idempotency keys stored in SQLite: survive restarts, scoped per client.

`begin()` decides atomically what to do with a request carrying `Idempotency-Key`:

* `new`         - first time: execute the operation, then `complete()` (or `abandon()` on error),
* `replay`      - same key and payload already finished: return the stored response,
* `in_progress` - same key and payload still running: wait and poll,
* `conflict`    - same key, different payload: reject (client error).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ai_router.storage.db import Database

DecisionKind = Literal["new", "replay", "in_progress", "conflict"]


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    kind: DecisionKind
    status_code: int | None = None
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    started_at: float | None = None


class IdempotencyRepository:
    def __init__(self, db: Database, *, ttl_seconds: float) -> None:
        self._db = db
        self._ttl = ttl_seconds

    async def begin(self, scope_key: str, fingerprint: str) -> IdempotencyDecision:
        now = time.time()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM idempotency WHERE scope_key = ?", (scope_key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["expires_at"] <= now:
                await conn.execute(
                    "INSERT OR REPLACE INTO idempotency (scope_key, fingerprint, status, "
                    "created_at, expires_at) VALUES (?, ?, 'in_progress', ?, ?)",
                    (scope_key, fingerprint, now, now + self._ttl),
                )
                return IdempotencyDecision("new")
        if row["fingerprint"] != fingerprint:
            return IdempotencyDecision("conflict")
        if row["status"] == "done":
            return IdempotencyDecision(
                "replay",
                status_code=row["status_code"],
                body=json.loads(row["body"]),
                headers=json.loads(row["headers"] or "{}"),
            )
        return IdempotencyDecision("in_progress", started_at=row["created_at"])

    async def complete(
        self, scope_key: str, *, status_code: int, body: Any, headers: dict[str, str]
    ) -> None:
        await self._db.execute(
            "UPDATE idempotency SET status = 'done', status_code = ?, body = ?, headers = ? "
            "WHERE scope_key = ?",
            (status_code, json.dumps(body), json.dumps(headers), scope_key),
        )

    async def abandon(self, scope_key: str) -> None:
        """Failures are not remembered - the client may retry with the same key."""
        await self._db.execute(
            "DELETE FROM idempotency WHERE scope_key = ? AND status = 'in_progress'", (scope_key,)
        )

    async def take_over(self, scope_key: str, fingerprint: str) -> bool:
        """Claim a stale `in_progress` entry (its request died, e.g. process restart)."""
        now = time.time()
        changed = await self._db.execute(
            "UPDATE idempotency SET created_at = ?, expires_at = ? "
            "WHERE scope_key = ? AND fingerprint = ? AND status = 'in_progress'",
            (now, now + self._ttl, scope_key, fingerprint),
        )
        return changed == 1

    async def purge_expired(self) -> int:
        return await self._db.execute(
            "DELETE FROM idempotency WHERE expires_at <= ?", (time.time(),)
        )
