"""RoutingService: verification of the outcome, fallback, timeouts, concurrency limit."""

import asyncio
import logging

import pytest
from pydantic_ai import ModelAPIError

from ai_router.errors import (
    AgentFailedError,
    LlmUnavailableError,
    RoutingTimeoutError,
    SmtpUnavailableError,
    TooManyRequestsError,
)
from ai_router.mail.sender import EmailDeliveryError, InMemoryEmailSender
from ai_router.routing.agent import build_agent
from ai_router.routing.departments import Department
from ai_router.routing.service import RoutingService
from tests.conftest import make_settings
from tests.helpers.scripted_model import ScriptedModel, Step, slow, text, tool_call

pytestmark = pytest.mark.anyio

MESSAGE = "Nie działa mi komputer"


def service(*steps: Step, mailer: InMemoryEmailSender | None = None, **overrides: object):  # type: ignore[no-untyped-def]
    settings = make_settings(**overrides)
    scripted = ScriptedModel(*steps)
    sender = mailer or InMemoryEmailSender()
    svc = RoutingService(
        agent=build_agent(settings, scripted.model),
        mailer=sender,
        settings=settings,
        model_name="scripted-model",
    )
    return svc, sender


async def route(svc: RoutingService, message: str = MESSAGE):  # type: ignore[no-untyped-def]
    return await svc.route(sender_email="jan@example.com", message=message, request_id="req-9")


async def test_success_outcome_comes_from_the_tool_record() -> None:
    svc, mailer = service(tool_call("help-desk@example.com", reason="Sprzęt"), text("Done"))
    outcome = await route(svc)
    assert outcome.sent.routed_by == "agent"
    assert outcome.sent.department is Department.HELP_DESK
    assert outcome.sent.reason == "Sprzęt"
    assert outcome.sent.message_id == mailer.sent[0]["Message-ID"]
    assert outcome.warnings == ()
    diag = outcome.diagnostics
    assert (diag.llm_requests, diag.tool_calls, diag.retries) == (2, 1, 0)


async def test_retries_are_counted() -> None:
    svc, _ = service(text("forwarded"), tool_call(), text())
    outcome = await route(svc)
    assert outcome.diagnostics.retries == 1
    assert outcome.diagnostics.llm_requests == 3


async def test_agent_failure_falls_back_to_other_department() -> None:
    svc, mailer = service(text(), text(), text())
    outcome = await route(svc)
    assert outcome.sent.routed_by == "fallback"
    assert outcome.sent.department is Department.OTHER
    assert outcome.warnings == ("agent_failed: model_behavior",)
    email = mailer.sent[0]
    assert email["To"] == "other@example.com"
    assert email["X-AI-Router-Routed-By"] == "fallback"
    assert email["Reply-To"] == "jan@example.com"
    assert MESSAGE in email.get_content()


async def test_agent_failure_without_fallback_is_an_error() -> None:
    svc, mailer = service(text(), text(), text(), routing_fallback_enabled=False)
    with pytest.raises(AgentFailedError):
        await route(svc)
    assert mailer.sent == []


async def test_unreachable_llm_falls_back() -> None:
    svc, mailer = service(ModelAPIError("scripted-model", "connection refused"))
    outcome = await route(svc)
    assert outcome.sent.routed_by == "fallback"
    assert outcome.warnings == ("agent_failed: llm_unavailable",)
    assert len(mailer.sent) == 1


async def test_unreachable_llm_without_fallback_is_503() -> None:
    svc, _ = service(ModelAPIError("m", "down"), routing_fallback_enabled=False)
    with pytest.raises(LlmUnavailableError):
        await route(svc)


async def test_timeout_before_sending_falls_back() -> None:
    svc, mailer = service(slow(tool_call(), 1.0), agent_timeout_seconds=0.1)
    outcome = await route(svc)
    assert outcome.sent.routed_by == "fallback"
    assert outcome.warnings == ("agent_failed: timeout",)
    assert [m["To"] for m in mailer.sent] == ["other@example.com"]


async def test_timeout_without_fallback_is_504() -> None:
    svc, _ = service(
        slow(tool_call(), 1.0), agent_timeout_seconds=0.1, routing_fallback_enabled=False
    )
    with pytest.raises(RoutingTimeoutError):
        await route(svc)


async def test_timeout_after_sending_is_still_a_success() -> None:
    svc, mailer = service(tool_call("it@example.com"), slow(text(), 1.0), agent_timeout_seconds=0.3)
    outcome = await route(svc)
    assert outcome.sent.routed_by == "agent"
    assert outcome.warnings == ("post_send_timeout",)
    assert [m["To"] for m in mailer.sent] == ["it@example.com"]  # no second (fallback) e-mail


async def test_smtp_failure_is_503_and_no_fallback_attempt() -> None:
    failing = InMemoryEmailSender(fail_with=EmailDeliveryError("down"))
    svc, _ = service(tool_call(), text(), mailer=failing)
    with pytest.raises(SmtpUnavailableError):
        await route(svc)


async def test_concurrency_limit_rejects_when_queue_wait_times_out() -> None:
    svc, _ = service(
        slow(tool_call(), 0.5),
        text(),
        tool_call(),
        text(),
        max_concurrent_agent_runs=1,
        agent_queue_timeout_seconds=0.05,
    )
    results = await asyncio.gather(route(svc), route(svc), return_exceptions=True)
    assert sum(isinstance(r, TooManyRequestsError) for r in results) == 1
    assert sum(not isinstance(r, BaseException) for r in results) == 1


async def test_message_content_is_not_logged_by_default(caplog: pytest.LogCaptureFixture) -> None:
    secret = "Moje hasło to Tajne123"
    svc, _ = service(tool_call(), text())
    with caplog.at_level(logging.INFO):
        await route(svc, message=secret)
    assert secret not in caplog.text
    assert any(r.getMessage() == "routing.completed" for r in caplog.records)


async def test_message_content_logged_only_when_enabled(caplog: pytest.LogCaptureFixture) -> None:
    secret = "Treść do debugowania"
    svc, _ = service(tool_call(), text(), log_message_content=True)
    with caplog.at_level(logging.INFO):
        await route(svc, message=secret)
    assert any(getattr(r, "message_text", None) == secret for r in caplog.records)
