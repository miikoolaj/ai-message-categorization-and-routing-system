import asyncio

import pytest

from ai_router.errors import IdempotencyConflictError
from ai_router.web.idempotency import IdempotencyStore

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def counter_operation():  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return f"result-{calls['n']}"

    return operation, calls


async def test_same_key_and_payload_replays_result() -> None:
    store = IdempotencyStore(ttl_seconds=60, max_entries=10)
    operation, calls = counter_operation()
    fp = IdempotencyStore.fingerprint({"a": 1})
    assert await store.execute("k", fp, operation) == ("result-1", False)
    assert await store.execute("k", fp, operation) == ("result-1", True)
    assert calls["n"] == 1


async def test_same_key_with_different_payload_is_rejected() -> None:
    store = IdempotencyStore(ttl_seconds=60, max_entries=10)
    operation, _ = counter_operation()
    await store.execute("k", IdempotencyStore.fingerprint({"a": 1}), operation)
    with pytest.raises(IdempotencyConflictError):
        await store.execute("k", IdempotencyStore.fingerprint({"a": 2}), operation)


async def test_concurrent_duplicates_run_the_operation_once() -> None:
    store = IdempotencyStore(ttl_seconds=60, max_entries=10)
    operation, calls = counter_operation()
    fp = IdempotencyStore.fingerprint({"a": 1})
    results = await asyncio.gather(*(store.execute("k", fp, operation) for _ in range(5)))
    assert calls["n"] == 1
    assert sorted(replayed for _, replayed in results) == [False, True, True, True, True]


async def test_failures_are_not_remembered() -> None:
    store = IdempotencyStore(ttl_seconds=60, max_entries=10)
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    fp = IdempotencyStore.fingerprint({})
    with pytest.raises(RuntimeError):
        await store.execute("k", fp, flaky)
    assert await store.execute("k", fp, flaky) == ("ok", False)


async def test_entries_expire_and_overflow_evicts_oldest() -> None:
    clock = Clock()
    store = IdempotencyStore(ttl_seconds=10, max_entries=2, clock=clock)
    operation, calls = counter_operation()
    fp = IdempotencyStore.fingerprint({})
    await store.execute("a", fp, operation)
    clock.now = 11
    assert (await store.execute("a", fp, operation))[1] is False  # expired -> executed again
    await store.execute("b", fp, operation)
    await store.execute("c", fp, operation)
    assert len(store) == 2  # "a" (least recently used) evicted
    assert calls["n"] == 4


def test_fingerprint_is_order_independent() -> None:
    assert IdempotencyStore.fingerprint({"a": 1, "b": 2}) == IdempotencyStore.fingerprint(
        {"b": 2, "a": 1}
    )
