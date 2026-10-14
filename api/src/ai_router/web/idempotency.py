"""Idempotent execution of `POST /api/v1/messages` (header `Idempotency-Key`).

The decision is stored in SQLite (survives restarts) and scoped per client, so two clients cannot
collide on the same key. Concurrent duplicates wait for the first request; a duplicate of a
request that died (e.g. process restart) takes over after the wait timeout.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_router.errors import IdempotencyConflictError, IdempotencyInProgressError
from ai_router.storage.idempotency import IdempotencyRepository


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def run_idempotent(
    repo: IdempotencyRepository,
    *,
    scope_key: str,
    payload_fingerprint: str,
    operation: Callable[[], Awaitable[StoredResponse]],
    wait_timeout: float,
    stale_after: float | None = None,
    poll_interval: float = 0.5,
) -> tuple[StoredResponse, bool]:
    """Run `operation` at most once per key. Returns `(response, replayed)`.

    A duplicate waits up to `wait_timeout` for the original request. An `in_progress` entry older
    than `stale_after` (default: `wait_timeout`) - longer than any request can legitimately run -
    belongs to a request that died and is taken over.
    """
    stale_threshold = wait_timeout if stale_after is None else stale_after
    deadline = time.monotonic() + wait_timeout
    decision = await repo.begin(scope_key, payload_fingerprint)
    while decision.kind == "in_progress":
        if time.monotonic() >= deadline:
            started = decision.started_at or time.time()
            stale = time.time() - started > stale_threshold
            if stale and await repo.take_over(scope_key, payload_fingerprint):
                break
            raise IdempotencyInProgressError(
                "A request with this Idempotency-Key is still being processed."
            )
        await asyncio.sleep(poll_interval)
        decision = await repo.begin(scope_key, payload_fingerprint)
    if decision.kind == "conflict":
        raise IdempotencyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    if decision.kind == "replay":
        return StoredResponse(decision.status_code or 200, decision.body, decision.headers), True
    try:
        response = await operation()
    except BaseException:
        await asyncio.shield(repo.abandon(scope_key))  # failures are not remembered
        raise
    await repo.complete(
        scope_key, status_code=response.status_code, body=response.body, headers=response.headers
    )
    return response, False
