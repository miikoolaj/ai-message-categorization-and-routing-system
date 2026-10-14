"""Tickets: the application layer around routing (sync and async modes, audit trail).

Every accepted request gets a ticket (server-generated id + human-friendly code `AIR-XXXXXXXX`)
stored in SQLite. The sync mode routes immediately; the async mode stores the ticket as `queued`
and lets `AsyncRoutingWorker` process it - the client polls `GET /api/v1/messages/{ticket_id}`.
"""

import contextlib
import hashlib
import hmac
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ai_router.config import Settings
from ai_router.errors import AppError
from ai_router.logging_config import log_event
from ai_router.routing.deps import SenderVerification
from ai_router.routing.service import RoutingOutcome, RoutingRequest, RoutingService
from ai_router.storage.tickets import TicketRecord, TicketRepository

logger = logging.getLogger(__name__)


class FeedbackSigner:
    """HMAC tokens for the "wrong department?" links (no login needed, cannot be forged)."""

    def __init__(self, secret: str) -> None:
        self._key = secret.encode()

    def sign(self, ticket_id: str) -> str:
        return hmac.new(self._key, ticket_id.encode(), hashlib.sha256).hexdigest()[:32]

    def verify(self, ticket_id: str, token: str) -> bool:
        return hmac.compare_digest(self.sign(ticket_id), token)


@dataclass(frozen=True, slots=True)
class Submission:
    request_id: str
    client_id: str
    sender_email: str
    sender_verification: SenderVerification
    message: str


def new_ticket_identity() -> tuple[str, str]:
    ticket_id = uuid.uuid4().hex
    return ticket_id, f"AIR-{ticket_id[:8].upper()}"


def error_info(exc: AppError) -> dict[str, object]:
    return {"type": exc.type_suffix, "title": exc.title, "detail": exc.detail}


class TicketService:
    def __init__(
        self,
        *,
        tickets: TicketRepository,
        routing: RoutingService,
        settings: Settings,
        signer: FeedbackSigner | None,
    ) -> None:
        self._tickets = tickets
        self._routing = routing
        self._settings = settings
        self._signer = signer
        self._notify_worker: Callable[[], None] = lambda: None

    def set_worker_notifier(self, notify: Callable[[], None]) -> None:
        self._notify_worker = notify

    def set_signer(self, signer: FeedbackSigner) -> None:
        """The signing secret comes from the database, available only after startup."""
        self._signer = signer

    @property
    def signer(self) -> FeedbackSigner | None:
        return self._signer

    def feedback_url(self, ticket_id: str) -> str | None:
        if self._signer is None or not self._settings.feedback_enabled:
            return None
        token = self._signer.sign(ticket_id)
        return f"{self._settings.public_base_url}/api/v1/feedback/{ticket_id}?token={token}"

    def _request(self, ticket: TicketRecord) -> RoutingRequest:
        return RoutingRequest(
            ticket_id=ticket.ticket_id,
            ticket_code=ticket.ticket_code,
            request_id=ticket.request_id,
            sender_email=ticket.sender_email,
            sender_verification=ticket.sender_verification,  # type: ignore[arg-type]
            message=ticket.message or "",
            feedback_url=self.feedback_url(ticket.ticket_id),
        )

    async def _create(self, submission: Submission, *, mode: str, status: str) -> TicketRecord:
        ticket_id, ticket_code = new_ticket_identity()
        await self._tickets.create(
            ticket_id=ticket_id,
            ticket_code=ticket_code,
            request_id=submission.request_id,
            client_id=submission.client_id,
            mode=mode,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            sender_email=submission.sender_email,
            sender_verification=submission.sender_verification,
            message=submission.message,
        )
        ticket = await self._tickets.get(ticket_id)
        assert ticket is not None  # noqa: S101 - just inserted
        return ticket

    async def route_sync(self, submission: Submission) -> RoutingOutcome:
        ticket = await self._create(submission, mode="sync", status="processing")
        return await self._run(ticket, wait_for_slot=False)

    async def submit_async(self, submission: Submission) -> TicketRecord:
        ticket = await self._create(submission, mode="async", status="queued")
        log_event(logger, logging.INFO, "ticket.queued", ticket=ticket.ticket_code)
        self._notify_worker()
        return ticket

    async def process_queued(self, ticket: TicketRecord) -> None:
        """Called by the async worker for a ticket already moved to `processing`."""
        with contextlib.suppress(AppError):  # already recorded on the ticket by _run()
            await self._run(ticket, wait_for_slot=True)

    async def _run(self, ticket: TicketRecord, *, wait_for_slot: bool) -> RoutingOutcome:
        try:
            outcome = await self._routing.route(self._request(ticket), wait_for_slot=wait_for_slot)
        except AppError as exc:
            await self._tickets.fail(ticket.ticket_id, error=error_info(exc))
            raise
        except Exception:
            await self._tickets.fail(
                ticket.ticket_id, error={"type": "internal-error", "title": "Internal server error"}
            )
            raise
        await self._tickets.complete(
            ticket.ticket_id,
            result=outcome.to_dict(),
            department=outcome.sent.department,
            routed_by=outcome.sent.routed_by,
            prompt_version=outcome.diagnostics.prompt_version,
        )
        return outcome

    async def get(self, ticket_id: str) -> TicketRecord | None:
        return await self._tickets.get(ticket_id)
