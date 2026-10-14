"""TicketService (sync/async), signed feedback links and the async worker."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from ai_router.errors import AgentFailedError
from ai_router.mail.composer import MailSettings
from ai_router.mail.gateway import InMemoryMailGateway
from ai_router.routing.agent import build_agent
from ai_router.routing.deps import AgentRuntime
from ai_router.routing.service import RoutingService
from ai_router.routing.tickets import FeedbackSigner, Submission, TicketService, new_ticket_identity
from ai_router.routing.worker import AsyncRoutingWorker
from ai_router.storage.db import Database
from ai_router.storage.tickets import TicketRepository
from tests.conftest import POLICY, make_settings
from tests.helpers.scripted_model import ScriptedModel, Step, text, tool_call

pytestmark = pytest.mark.anyio

SUBMISSION = Submission(
    request_id="req-1",
    client_id="anonymous",
    sender_email="jan@example.com",
    sender_verification="none",
    message="Nie działa mi komputer",
)


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


def ticket_service(
    db: Database, *steps: Step, **overrides: object
) -> tuple[TicketService, TicketRepository, InMemoryMailGateway]:
    settings = make_settings(**overrides)
    routing_agent = build_agent(settings, ScriptedModel(*steps).model, POLICY)
    gateway = InMemoryMailGateway()
    runtime = AgentRuntime(
        policy=POLICY,
        mail_settings=MailSettings.from_settings(settings),
        model_name="scripted",
        prompt_version=routing_agent.prompt_version,
        confidence_threshold=0.5,
        low_confidence_action="flag",
    )
    routing = RoutingService(
        routing_agent=routing_agent, gateway=gateway, runtime=runtime, settings=settings
    )
    repo = TicketRepository(db)
    service = TicketService(
        tickets=repo, routing=routing, settings=settings, signer=FeedbackSigner("secret")
    )
    return service, repo, gateway


def test_ticket_identity_and_feedback_signature() -> None:
    ticket_id, code = new_ticket_identity()
    assert len(ticket_id) == 32 and code == f"AIR-{ticket_id[:8].upper()}"
    signer = FeedbackSigner("k")
    assert signer.verify(ticket_id, signer.sign(ticket_id))
    assert not signer.verify(ticket_id, "0" * 32)
    assert not FeedbackSigner("other").verify(ticket_id, signer.sign(ticket_id))


async def test_sync_routing_stores_the_result(db: Database) -> None:
    service, repo, gateway = ticket_service(db, tool_call("help-desk@example.com"), text())
    outcome = await service.route_sync(SUBMISSION)
    ticket = await repo.get(outcome.request.ticket_id)
    assert ticket is not None and ticket.status == "routed"
    assert ticket.result is not None and ticket.result["department"] == "help-desk@example.com"
    url = service.feedback_url(ticket.ticket_id)
    assert url is not None and url.startswith("http://test/api/v1/feedback/")
    assert url in gateway.messages[0].get_body(("plain",)).get_content()  # type: ignore[union-attr]


async def test_failed_routing_is_recorded_on_the_ticket(db: Database) -> None:
    service, repo, _ = ticket_service(db, text(), text(), text(), routing_fallback_enabled=False)
    with pytest.raises(AgentFailedError):
        await service.route_sync(SUBMISSION)
    counts = await repo.counts_by_status()
    assert counts["failed"] == 1


async def test_async_ticket_is_processed_by_the_worker(db: Database) -> None:
    service, repo, gateway = ticket_service(db, tool_call("kadry@example.com"), text())
    worker = AsyncRoutingWorker(repo, service, concurrency=1, poll_interval=0.01)
    service.set_worker_notifier(worker.notify)
    ticket = await service.submit_async(SUBMISSION)
    assert ticket.status == "queued" and ticket.mode == "async"
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(200):
            current = await repo.get(ticket.ticket_id)
            if current is not None and current.status == "routed":
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert current is not None and current.status == "routed"
    assert [m["To"] for m in gateway.messages] == ["kadry@example.com"]
