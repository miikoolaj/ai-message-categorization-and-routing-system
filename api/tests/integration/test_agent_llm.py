"""The real agent on the real model (run: make test-llm). Slow and probabilistic by nature."""

import pytest
from pydantic_ai import models

from ai_router.config import Settings
from ai_router.llm.model import build_ollama_model
from ai_router.mail.composer import MailSettings
from ai_router.mail.gateway import InMemoryMailGateway
from ai_router.routing.agent import build_agent
from ai_router.routing.deps import AgentRuntime
from ai_router.routing.policy import load_policy
from ai_router.routing.service import RoutingRequest, RoutingService

pytestmark = [pytest.mark.llm, pytest.mark.anyio]

CASES = [
    ("Nie działa mi komputer", "help-desk@example.com"),
    ("Chciałbym zgłosić urlop na jutro", "kadry@example.com"),
    ("Serwer produkcyjny nie odpowiada, nikt w firmie nie może pracować", "it@example.com"),
]


@pytest.mark.parametrize(("message", "expected"), CASES)
async def test_real_model_routes_clear_cases(
    message: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    settings = Settings(routing_fallback_enabled=False)  # an agent failure must fail the test
    policy = load_policy(settings.routing_policy_file)
    handle = build_ollama_model(settings)
    routing_agent = build_agent(settings, handle.model, policy)
    gateway = InMemoryMailGateway()
    runtime = AgentRuntime(
        policy=policy,
        mail_settings=MailSettings.from_settings(settings),
        model_name=handle.name,
        prompt_version=routing_agent.prompt_version,
        confidence_threshold=settings.confidence_threshold,
        low_confidence_action=settings.low_confidence_action,
    )
    service = RoutingService(
        routing_agent=routing_agent, gateway=gateway, runtime=runtime, settings=settings
    )
    try:
        outcome = await service.route(
            RoutingRequest(
                ticket_id="f" * 32,
                ticket_code="AIR-LLMTEST0",
                request_id="llm",
                sender_email="llm-test@example.com",
                sender_verification="none",
                message=message,
            )
        )
    finally:
        await handle.close()
    assert outcome.sent.routed_by == "agent"
    assert outcome.sent.department == expected, outcome.sent.reason
    assert len(gateway.messages) == 1
