"""Per-run dependencies of the routing agent (injected into the tool via `RunContext.deps`)."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ai_router.mail.composer import MailSettings, RoutedBy
from ai_router.routing.policy import RoutingPolicy

if TYPE_CHECKING:
    from ai_router.mail.gateway import MailGateway

SenderVerification = Literal["none", "client", "sso"]
DeliveryStatus = Literal["sent", "queued"]
LowConfidenceAction = Literal["flag", "other"]


@dataclass(frozen=True, slots=True)
class SentEmail:
    """Record of an e-mail accepted for delivery - the source of truth for the HTTP response."""

    ticket_id: str
    department: str  # where the e-mail went
    chosen_department: str  # what the agent chose (differs only after a low-confidence redirect)
    reply_to: str
    subject: str
    reason: str
    message_id: str
    routed_by: RoutedBy
    confidence: float | None
    needs_review: bool
    delivery: DeliveryStatus


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Configuration shared by all runs (built once at startup)."""

    policy: RoutingPolicy
    mail_settings: MailSettings
    model_name: str
    prompt_version: str
    confidence_threshold: float
    low_confidence_action: LowConfidenceAction


@dataclass(slots=True)
class RoutingDeps:
    """Everything the agent's tool needs for ONE request (the agent itself is shared)."""

    runtime: AgentRuntime
    gateway: "MailGateway"
    ticket_id: str
    ticket_code: str
    request_id: str
    sender_email: str  # validated address from the request (or identity) -> Reply-To
    sender_verification: SenderVerification
    original_message: str  # forwarded verbatim in the e-mail body
    feedback_url: str | None = None
    sent: SentEmail | None = None  # filled by the tool after the gateway accepted the e-mail
    submission: "asyncio.Future[DeliveryStatus] | None" = None  # in-flight hand-over (shielded)
    tool_attempts: int = 0
    tool_duplicates: int = 0
