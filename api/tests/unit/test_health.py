import pytest

from ai_router.health import ReadinessChecker, WarmupState
from tests.conftest import FakeSmtpServer, make_settings
from tests.helpers.fakes import FakeOllamaAdmin

pytestmark = pytest.mark.anyio


def checker(
    smtp: FakeSmtpServer | None, admin: FakeOllamaAdmin, warm: str = "warm"
) -> ReadinessChecker:
    settings = make_settings(
        smtp_host=smtp.host if smtp else "127.0.0.1", smtp_port=smtp.port if smtp else 1
    )
    return ReadinessChecker(
        settings=settings,
        ollama_admin=admin,
        warmup=WarmupState(status=warm),  # type: ignore[arg-type]
        check_timeout=0.5,
    )


async def test_ready_when_all_dependencies_are_up(fake_smtp_server: FakeSmtpServer) -> None:
    report = await checker(fake_smtp_server, FakeOllamaAdmin()).check()
    assert report.status == "ready"
    assert {k: v.status for k, v in report.components.items()} == {
        "ollama": "up",
        "model": "up",
        "model_warm": "up",
        "smtp": "up",
    }
    assert report.components["model"].name == "qwen3:4b-instruct-2507-q4_K_M"


async def test_missing_model_means_not_ready(fake_smtp_server: FakeSmtpServer) -> None:
    report = await checker(fake_smtp_server, FakeOllamaAdmin(model_present=False)).check()
    assert report.status == "not_ready"
    assert report.components["model"].detail == "model not found"


async def test_unreachable_ollama_and_smtp(fake_smtp_server: FakeSmtpServer) -> None:
    report = await checker(None, FakeOllamaAdmin(reachable=False)).check()
    assert report.status == "not_ready"
    assert report.components["ollama"].status == "down"
    assert report.components["smtp"].status == "down"


async def test_cold_model_does_not_block_readiness(fake_smtp_server: FakeSmtpServer) -> None:
    report = await checker(fake_smtp_server, FakeOllamaAdmin(), warm="pending").check()
    assert report.status == "ready"
    assert report.components["model_warm"].status == "down"
