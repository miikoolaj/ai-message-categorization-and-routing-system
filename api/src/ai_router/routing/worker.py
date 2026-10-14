"""Background worker for the asynchronous mode: takes queued tickets and routes them."""

import asyncio
import contextlib
import logging

from ai_router.logging_config import log_event
from ai_router.routing.tickets import TicketService
from ai_router.storage.db import StorageError
from ai_router.storage.tickets import TicketRepository

logger = logging.getLogger(__name__)


class AsyncRoutingWorker:
    def __init__(
        self,
        tickets: TicketRepository,
        service: TicketService,
        *,
        concurrency: int,
        poll_interval: float = 1.0,
    ) -> None:
        self._tickets = tickets
        self._service = service
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._wake = asyncio.Event()

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        await asyncio.gather(*(self._loop(n) for n in range(self._concurrency)))

    async def _loop(self, worker_no: int) -> None:
        while True:
            try:
                ticket = await self._tickets.claim_next_queued()
            except StorageError as exc:
                log_event(logger, logging.ERROR, "worker.storage_error", error=str(exc))
                ticket = None
            if ticket is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                self._wake.clear()
                continue
            log_event(
                logger,
                logging.INFO,
                "worker.processing",
                ticket=ticket.ticket_code,
                worker=worker_no,
            )
            try:
                await self._service.process_queued(ticket)
            except Exception as exc:  # never let one ticket kill the worker
                log_event(
                    logger,
                    logging.ERROR,
                    "worker.unexpected_error",
                    ticket=ticket.ticket_code,
                    error=repr(exc),
                )
