"""In-memory idempotency for `POST /api/v1/messages` (header `Idempotency-Key`).

The same key with the same payload returns the stored response instead of sending a second
e-mail. The same key with a different payload is rejected. Failed operations are not stored,
so the client can retry with the same key. State is per process (PoC); production would use Redis.
"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_router.errors import IdempotencyConflictError


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    result: Any = None


class IdempotencyStore:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    @staticmethod
    def fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def __len__(self) -> int:
        return len(self._entries)

    async def execute[T](
        self, key: str, fingerprint: str, operation: Callable[[], Awaitable[T]]
    ) -> tuple[T, bool]:
        """Run `operation` once per key. Returns `(result, replayed)`."""
        self._evict_expired()
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(fingerprint=fingerprint, expires_at=self._clock() + self._ttl)
            self._entries[key] = entry
            self._evict_overflow()
        elif entry.fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        self._entries.move_to_end(key)

        async with entry.lock:  # concurrent duplicates wait for the first request
            if entry.result is not None:
                return entry.result, True
            try:
                result = await operation()
            except BaseException:
                if self._entries.get(key) is entry:
                    del self._entries[key]  # do not remember failures
                raise
            entry.result = result
            return result, False

    def _evict_expired(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]

    def _evict_overflow(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # least recently used
