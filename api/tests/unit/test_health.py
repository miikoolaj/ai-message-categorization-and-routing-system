from collections.abc import AsyncIterator

import pytest

from ai_router.health import ReadinessChecker, WarmupState, describe_failure
from ai_router.storage.db import Database, StorageError
from tests.conftest import FakeSmtpServer, make_settings
from tests.helpers.fakes import FakeOllamaAdmin

pytestmark = pytest.mark.anyio


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


def checker(
    smtp: FakeSmtpServer | None,
    admin: FakeOllamaAdmin | None,
    db: Database,
    warm: str = "warm",
    **overrides: object,
) -> ReadinessChecker:
    settings = make_settings(
        smtp_host=smtp.host if smtp else "127.0.0.1",
        smtp_port=smtp.port if smtp else 1,
        **overrides,
    )
    return ReadinessChecker(
        settings=settings,
        ollama_admin=admin,
        database=db,
        warmup=WarmupState(status=warm),  # type: ignore[arg-type]
        check_timeout=0.5,
    )


async def test_ready_when_all_dependencies_are_up(
    fake_smtp_server: FakeSmtpServer, db: Database
) -> None:
    report = await checker(fake_smtp_server, FakeOllamaAdmin(), db).check()
    assert report.status == "ready"
    assert {k: v.status for k, v in report.components.items()} == dict.fromkeys(
        ("ollama", "model", "model_warm", "smtp", "storage"), "up"
    )


async def test_model_without_tool_support_is_not_ready(
    fake_smtp_server: FakeSmtpServer, db: Database
) -> None:
    admin = FakeOllamaAdmin(capabilities={"completion"})
    report = await checker(fake_smtp_server, admin, db).check()
    assert report.status == "not_ready"
    assert report.components["model"].detail == "model does not support tool calling"


async def test_missing_model_and_unreachable_dependencies(db: Database) -> None:
    report = await checker(None, FakeOllamaAdmin(reachable=False), db).check()
    assert report.status == "not_ready"
    assert report.components["ollama"].detail == "connection failed"  # no host names leak
    assert "secret-host" not in report.model_dump_json()
    assert report.components["smtp"].status == "down"


async def test_fake_backend_skips_ollama_checks(
    fake_smtp_server: FakeSmtpServer, db: Database
) -> None:
    report = await checker(fake_smtp_server, None, db).check()
    assert report.status == "ready"
    assert report.components["model"].name == "fake-rules"


async def test_storage_down_is_not_ready(fake_smtp_server: FakeSmtpServer) -> None:
    report = await checker(fake_smtp_server, FakeOllamaAdmin(), Database(":memory:")).check()
    assert report.components["storage"].status == "down"
    assert report.status == "not_ready"


async def test_cold_model_blocks_only_with_strict_readiness(
    fake_smtp_server: FakeSmtpServer, db: Database
) -> None:
    assert (
        await checker(fake_smtp_server, FakeOllamaAdmin(), db, warm="pending").check()
    ).status == "ready"
    strict = checker(
        fake_smtp_server, FakeOllamaAdmin(), db, warm="pending", readiness_require_warm=True
    )
    assert (await strict.check()).status == "not_ready"


def test_describe_failure_is_coarse() -> None:
    assert describe_failure(TimeoutError()) == "timeout"
    assert describe_failure(ConnectionRefusedError("10.0.0.1:25")) == "connection failed"
    assert describe_failure(StorageError("disk full at /app/data")) == "storage error"
    assert describe_failure(ValueError("secret")) == "ValueError"
