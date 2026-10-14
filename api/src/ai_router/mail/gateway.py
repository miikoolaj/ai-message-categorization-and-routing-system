"""Hand-over point between the agent's tool and e-mail delivery.

The tool does not talk SMTP. It *submits* the message to a gateway, which accepts it durably and
delivers it (production: the transactional outbox in `mail/outbox.py`). Two consequences:

* an agent timeout can never cut an SMTP transaction in half (no duplicate delivery), and
* an SMTP outage does not lose the request - the message waits in the outbox and is retried.
"""

import asyncio
from dataclasses import replace
from email.message import EmailMessage
from typing import Protocol

from ai_router.routing.deps import DeliveryStatus, SentEmail


class MailGatewayError(Exception):
    """The gateway could not accept the message (e.g. the database is unavailable)."""


class MailGateway(Protocol):
    async def submit(self, message: EmailMessage, record: SentEmail) -> DeliveryStatus:
        """Accept the message durably; return `sent` if already delivered, else `queued`."""
        ...

    async def find(self, ticket_id: str) -> SentEmail | None:
        """The record of a message accepted for `ticket_id` (with its current delivery status)."""
        ...


class InMemoryMailGateway:
    """Records messages instead of delivering them (tests, evaluation, prompt-cache warm-up).

    `ack_delay` simulates a slow acknowledgement: the message is already accepted, but `submit`
    returns late - exactly the situation that used to cause duplicate deliveries on timeout.
    """

    def __init__(
        self,
        *,
        delivery: DeliveryStatus = "sent",
        fail_with: Exception | None = None,
        ack_delay: float = 0.0,
    ) -> None:
        self.delivery: DeliveryStatus = delivery
        self.fail_with = fail_with
        self.ack_delay = ack_delay
        self.messages: list[EmailMessage] = []
        self.records: dict[str, SentEmail] = {}

    async def submit(self, message: EmailMessage, record: SentEmail) -> DeliveryStatus:
        if self.fail_with is not None:
            raise self.fail_with
        self.messages.append(message)
        self.records[record.ticket_id] = record
        if self.ack_delay:
            await asyncio.sleep(self.ack_delay)
        return self.delivery

    async def find(self, ticket_id: str) -> SentEmail | None:
        record = self.records.get(ticket_id)
        return replace(record, delivery=self.delivery) if record else None
