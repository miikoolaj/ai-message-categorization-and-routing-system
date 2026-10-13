"""The real agent on the real model (run: make test-llm). Slow and probabilistic by nature."""

import pytest
from pydantic_ai import models

from ai_router.config import Settings
from ai_router.llm.model import build_model
from ai_router.mail.sender import InMemoryEmailSender
from ai_router.routing.agent import build_agent
from ai_router.routing.service import RoutingService

pytestmark = [pytest.mark.llm, pytest.mark.anyio]

CASES = [
    ("Nie działa mi komputer", "help-desk@example.com"),
    ("Chciałbym zgłosić urlop na jutro", "kadry@example.com"),
    ("Serwer produkcyjny nie odpowiada, nikt w firmie nie może pracować", "it@example.com"),
]


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> tuple[RoutingService, InMemoryEmailSender]:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    settings = Settings(routing_fallback_enabled=False)  # an agent failure must fail the test
    mailer = InMemoryEmailSender()
    agent = build_agent(settings, build_model(settings))
    svc = RoutingService(agent=agent, mailer=mailer, settings=settings, model_name="live")
    return svc, mailer


@pytest.mark.parametrize(("message", "expected"), CASES)
async def test_real_model_routes_clear_cases(
    service: tuple[RoutingService, InMemoryEmailSender], message: str, expected: str
) -> None:
    svc, mailer = service
    outcome = await svc.route(
        sender_email="llm-test@example.com", message=message, request_id="llm"
    )
    assert outcome.sent.routed_by == "agent"
    assert outcome.sent.department.value == expected, outcome.sent.reason
    assert len(mailer.sent) == 1
