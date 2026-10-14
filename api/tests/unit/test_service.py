"""RoutingService: source of truth, fallback, failure classification, timeouts, concurrency."""

import asyncio
import logging
from typing import Any

import pytest
from pydantic_ai import ModelAPIError
from pydantic_ai.exceptions import ModelHTTPError

from ai_router.errors import (
    AgentFailedError,
    LlmMisconfiguredError,
    LlmUnavailableError,
    RoutingTimeoutError,
    StorageUnavailableError,
    TooManyRequestsError,
)
from ai_router.mail.composer import MailSettings
from ai_router.mail.gateway import InMemoryMailGateway, MailGatewayError
from ai_router.routing.agent import build_agent
from ai_router.routing.deps import AgentRuntime
from ai_router.routing.service import RoutingRequest, RoutingService, classify_http_error
from tests.conftest import POLICY, make_settings
from tests.helpers.scripted_model import ScriptedModel, Step, slow, text, tool_call

pytestmark = pytest.mark.anyio

MESSAGE = "Nie działa mi komputer"


def service(
    *steps: Step, gateway: InMemoryMailGateway | None = None, **overrides: Any
) -> tuple[RoutingService, InMemoryMailGateway]:
    settings = make_settings(**overrides)
    routing_agent = build_agent(settings, ScriptedModel(*steps).model, POLICY)
    mailbox = gateway or InMemoryMailGateway()
    runtime = AgentRuntime(
        policy=POLICY,
        mail_settings=MailSettings.from_settings(settings),
        model_name="scripted-model",
        prompt_version=routing_agent.prompt_version,
        confidence_threshold=settings.confidence_threshold,
        low_confidence_action=settings.low_confidence_action,
    )
    svc = RoutingService(
        routing_agent=routing_agent, gateway=mailbox, runtime=runtime, settings=settings
    )
    return svc, mailbox


def request(ticket: str = "a" * 32, message: str = MESSAGE) -> RoutingRequest:
    return RoutingRequest(
        ticket_id=ticket,
        ticket_code="AIR-AAAAAAAA",
        request_id="req-9",
        sender_email="jan@example.com",
        sender_verification="none",
        message=message,
    )


async def test_success_outcome_comes_from_the_gateway_record() -> None:
    svc, mailbox = service(tool_call("help-desk@example.com", reason="Sprzęt"), text("Done"))
    outcome = await svc.route(request())
    assert outcome.sent.routed_by == "agent"
    assert outcome.sent.department == "help-desk@example.com"
    assert outcome.sent.message_id == mailbox.messages[0]["Message-ID"]
    assert outcome.warnings == ()
    diag = outcome.diagnostics
    assert (diag.llm_requests, diag.tool_calls, diag.tool_duplicates, diag.retries) == (2, 1, 0, 0)
    assert diag.prompt_version == svc.prompt_version
    data = outcome.to_dict()
    assert data["status"] == "routed" and data["email"]["delivery"] == "sent"


async def test_timeout_during_slow_acknowledgement_does_not_duplicate_delivery() -> None:
    """Regression for the duplicate delivery found in the review (report, experiment 1)."""
    slow_ack = InMemoryMailGateway(ack_delay=1.0)
    svc, mailbox = service(
        tool_call("it@example.com"), text(), gateway=slow_ack, agent_timeout_seconds=0.3
    )
    outcome = await svc.route(request())
    assert [m["To"] for m in mailbox.messages] == ["it@example.com"]  # exactly one e-mail
    assert outcome.sent.routed_by == "agent"
    assert outcome.sent.department == "it@example.com"
    assert "post_send_timeout" in outcome.warnings


async def test_queued_delivery_is_reported() -> None:
    svc, _ = service(tool_call(), text(), gateway=InMemoryMailGateway(delivery="queued"))
    outcome = await svc.route(request())
    assert outcome.sent.delivery == "queued"
    assert "delivery_queued" in outcome.warnings


async def test_agent_failure_falls_back_to_the_policy_fallback() -> None:
    svc, mailbox = service(text(), text(), text())
    outcome = await svc.route(request())
    assert outcome.sent.routed_by == "fallback"
    assert outcome.sent.department == POLICY.fallback.address
    assert outcome.sent.needs_review is True
    assert outcome.warnings[0] == "agent_failed: model_behavior"
    email = mailbox.messages[0]
    assert email["X-AI-Router-Routed-By"] == "fallback"
    assert email["Reply-To"] == "jan@example.com"


async def test_agent_failure_without_fallback_is_an_error() -> None:
    svc, mailbox = service(text(), text(), text(), routing_fallback_enabled=False)
    with pytest.raises(AgentFailedError):
        await svc.route(request())
    assert mailbox.messages == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModelAPIError("m", "connection refused"), LlmUnavailableError),
        (ModelHTTPError(404, "m", {"error": "model not found"}), LlmMisconfiguredError),
        (ModelHTTPError(400, "m", {"error": "does not support tools"}), LlmMisconfiguredError),
        (
            ModelHTTPError(
                500, "m", {"error": "llama-server returned invalid tool call arguments"}
            ),
            AgentFailedError,
        ),
    ],
    ids=["unreachable", "missing-model", "no-tools", "broken-tool-call"],
)
async def test_failures_are_classified(error: Exception, expected: type[Exception]) -> None:
    svc, _ = service(error, routing_fallback_enabled=False)
    with pytest.raises(expected):
        await svc.route(request())


def test_classify_http_error() -> None:
    body = {"error": {"message": "llama-server returned invalid tool call arguments"}}
    assert classify_http_error(ModelHTTPError(500, "m", body)) == "model_behavior"
    assert classify_http_error(ModelHTTPError(404, "m", None)) == "llm_misconfigured"
    assert classify_http_error(ModelHTTPError(503, "m", None)) == "llm_unavailable"


async def test_unreachable_llm_falls_back_with_cause() -> None:
    svc, mailbox = service(ModelAPIError("m", "down"))
    outcome = await svc.route(request())
    assert outcome.warnings[0] == "agent_failed: llm_unavailable"
    assert len(mailbox.messages) == 1


async def test_timeout_before_handover_falls_back_or_is_504() -> None:
    svc, _ = service(slow(tool_call(), 1.0), agent_timeout_seconds=0.1)
    outcome = await svc.route(request())
    assert outcome.sent.routed_by == "fallback"
    assert outcome.warnings[0] == "agent_failed: timeout"
    svc, _ = service(
        slow(tool_call(), 1.0), agent_timeout_seconds=0.1, routing_fallback_enabled=False
    )
    with pytest.raises(RoutingTimeoutError):
        await svc.route(request())


async def test_storage_failure_is_503() -> None:
    failing = InMemoryMailGateway(fail_with=MailGatewayError("db down"))
    svc, _ = service(tool_call(), text(), gateway=failing)
    with pytest.raises(StorageUnavailableError) as info:
        await svc.route(request())
    assert info.value.detail is None  # no internal details for API clients


async def test_concurrency_limit_rejects_when_queue_wait_times_out() -> None:
    svc, _ = service(
        slow(tool_call(), 0.5),
        text(),
        tool_call(),
        text(),
        max_concurrent_agent_runs=1,
        agent_queue_timeout_seconds=0.05,
    )
    results = await asyncio.gather(
        svc.route(request("a" * 32)), svc.route(request("b" * 32)), return_exceptions=True
    )
    assert sum(isinstance(r, TooManyRequestsError) for r in results) == 1


async def test_async_worker_mode_waits_for_a_slot_instead_of_failing() -> None:
    svc, mailbox = service(
        slow(tool_call(), 0.3),
        text(),
        tool_call(),
        text(),
        max_concurrent_agent_runs=1,
        agent_queue_timeout_seconds=0.05,
    )
    first, second = await asyncio.gather(
        svc.route(request("a" * 32), wait_for_slot=True),
        svc.route(request("b" * 32), wait_for_slot=True),
    )
    assert len(mailbox.messages) == 2
    assert max(first.diagnostics.queue_wait_ms, second.diagnostics.queue_wait_ms) >= 200


async def test_context_near_limit_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    svc, _ = service(tool_call(), text(), context_warning_ratio=0.0001)  # any prompt is "near"
    with caplog.at_level(logging.WARNING):
        await svc.route(request())
    assert any(r.getMessage() == "llm.context_near_limit" for r in caplog.records)


async def test_message_content_is_not_logged_by_default(caplog: pytest.LogCaptureFixture) -> None:
    secret = "Moje hasło to Tajne123"
    svc, _ = service(tool_call(), text())
    with caplog.at_level(logging.INFO):
        await svc.route(request(message=secret))
    assert secret not in caplog.text
    assert any(r.getMessage() == "routing.completed" for r in caplog.records)
