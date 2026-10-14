import asyncio
from collections.abc import AsyncIterator

import pytest

from ai_router.errors import IdempotencyConflictError, IdempotencyInProgressError
from ai_router.storage.db import Database
from ai_router.storage.idempotency import IdempotencyRepository
from ai_router.web.idempotency import StoredResponse, fingerprint, run_idempotent

pytestmark = pytest.mark.anyio


@pytest.fixture
async def repo() -> AsyncIterator[IdempotencyRepository]:
    db = Database(":memory:")
    await db.connect()
    yield IdempotencyRepository(db, ttl_seconds=60)
    await db.close()


def counting_operation(delay: float = 0.0):  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    async def operation() -> StoredResponse:
        calls["n"] += 1
        await asyncio.sleep(delay)
        return StoredResponse(200, {"n": calls["n"]})

    return operation, calls


async def test_replay_returns_the_stored_response(repo: IdempotencyRepository) -> None:
    operation, calls = counting_operation()
    first = await run_idempotent(
        repo, scope_key="c:k", payload_fingerprint="fp", operation=operation, wait_timeout=1
    )
    second = await run_idempotent(
        repo, scope_key="c:k", payload_fingerprint="fp", operation=operation, wait_timeout=1
    )
    assert first == (StoredResponse(200, {"n": 1}), False)
    assert second[1] is True and second[0].body == {"n": 1}
    assert calls["n"] == 1


async def test_different_payload_is_a_conflict(repo: IdempotencyRepository) -> None:
    operation, _ = counting_operation()
    await run_idempotent(
        repo, scope_key="c:k", payload_fingerprint="a", operation=operation, wait_timeout=1
    )
    with pytest.raises(IdempotencyConflictError):
        await run_idempotent(
            repo, scope_key="c:k", payload_fingerprint="b", operation=operation, wait_timeout=1
        )


async def test_concurrent_duplicate_waits_for_the_first(repo: IdempotencyRepository) -> None:
    operation, calls = counting_operation(delay=0.2)
    results = await asyncio.gather(
        *(
            run_idempotent(
                repo,
                scope_key="c:k",
                payload_fingerprint="fp",
                operation=operation,
                wait_timeout=2,
                poll_interval=0.02,
            )
            for _ in range(3)
        )
    )
    assert calls["n"] == 1
    assert sorted(replayed for _, replayed in results) == [False, True, True]


async def test_still_running_duplicate_gets_409(repo: IdempotencyRepository) -> None:
    operation, _ = counting_operation(delay=0.5)
    first = asyncio.create_task(
        run_idempotent(
            repo, scope_key="c:k", payload_fingerprint="fp", operation=operation, wait_timeout=5
        )
    )
    await asyncio.sleep(0.05)
    with pytest.raises(IdempotencyInProgressError):
        await run_idempotent(
            repo,
            scope_key="c:k",
            payload_fingerprint="fp",
            operation=operation,
            wait_timeout=0.1,
            stale_after=5,
            poll_interval=0.02,
        )
    await first


async def test_abandoned_in_progress_entry_is_taken_over(repo: IdempotencyRepository) -> None:
    await repo.begin("c:k", "fp")  # a request that died without completing
    operation, calls = counting_operation()
    response, replayed = await run_idempotent(
        repo,
        scope_key="c:k",
        payload_fingerprint="fp",
        operation=operation,
        wait_timeout=0.05,
        stale_after=0.01,
        poll_interval=0.01,
    )
    assert calls["n"] == 1 and replayed is False and response.body == {"n": 1}


async def test_failures_are_not_remembered(repo: IdempotencyRepository) -> None:
    attempts = {"n": 0}

    async def flaky() -> StoredResponse:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        return StoredResponse(200, {"ok": True})

    with pytest.raises(RuntimeError):
        await run_idempotent(
            repo, scope_key="c:k", payload_fingerprint="fp", operation=flaky, wait_timeout=1
        )
    response, replayed = await run_idempotent(
        repo, scope_key="c:k", payload_fingerprint="fp", operation=flaky, wait_timeout=1
    )
    assert response.body == {"ok": True} and replayed is False


def test_fingerprint_is_order_independent() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
