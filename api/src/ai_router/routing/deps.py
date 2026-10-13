"""Per-run dependencies of the routing agent (injected into tools via `RunContext.deps`)."""

from dataclasses import dataclass

from ai_router.mail.composer import MailSettings, RoutedBy
from ai_router.mail.sender import EmailSender
from ai_router.routing.departments import Department


@dataclass(frozen=True, slots=True)
class SentEmail:
    """Record of an e-mail that was actually sent - the source of truth for the HTTP response."""

    department: Department
    to: str
    reply_to: str
    subject: str
    reason: str
    message_id: str
    routed_by: RoutedBy


@dataclass(slots=True)
class RoutingDeps:
    """Everything the agent's tool needs for ONE request.

    Created per request; the agent itself is shared and stateless.
    """

    request_id: str
    sender_email: str  # validated address from the HTTP request -> Reply-To
    original_message: str  # forwarded verbatim in the e-mail body
    mailer: EmailSender
    mail_settings: MailSettings
    model_name: str  # for the X-AI-Router-Model header
    sent: SentEmail | None = None  # filled by the tool after a successful send
    send_in_progress: bool = False  # "exactly once" guard (set before the first await)
    tool_attempts: int = 0  # diagnostics
