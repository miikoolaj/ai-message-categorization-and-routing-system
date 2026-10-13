"""Building the routed e-mail. Pure functions only - no I/O, no AI.

Security-relevant values are set here by code, never by the LLM:
`Reply-To` (sender's address from the HTTP request), the original message (verbatim),
`From`, `Message-ID` and the diagnostic `X-AI-Router-*` headers.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import TYPE_CHECKING, Literal

from ai_router.routing.departments import Department

if TYPE_CHECKING:
    from ai_router.config import Settings

RoutedBy = Literal["agent", "fallback"]

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MailSettings:
    from_address: str
    subject_prefix: str
    message_id_domain: str

    @classmethod
    def from_settings(cls, settings: "Settings") -> "MailSettings":
        return cls(
            from_address=settings.mail_from,
            subject_prefix=settings.mail_subject_prefix,
            message_id_domain=settings.mail_message_id_domain,
        )


def sanitize_header(value: str, max_length: int = 120) -> str:
    """Make a value safe for a single-line e-mail header.

    Removes control characters (CR/LF would allow header injection), collapses whitespace and
    truncates to `max_length` characters, preferably at a word boundary.
    """
    cleaned = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", value)).strip()
    if len(cleaned) <= max_length:
        return cleaned
    truncated = cleaned[:max_length]
    last_space = truncated.rfind(" ")
    if last_space >= max_length // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def _body(
    *,
    sender_email: str,
    department: Department,
    reason: str,
    original_message: str,
    request_id: str,
    routed_by: RoutedBy,
    received_at: datetime,
) -> str:
    mode = (
        "agent AI (automatyczne przypisanie)"
        if routed_by == "agent"
        else "fallback (automatyczne przypisanie nie powiodło się - wymagana weryfikacja)"
    )
    return (
        "Nowe zgłoszenie przekazane automatycznie przez AI Router.\n"
        "\n"
        f"Nadawca:      {sender_email}  (odpowiedz na tę wiadomość, aby odpisać nadawcy)\n"
        f"Dział:        {department.value}\n"
        f"Uzasadnienie: {reason}\n"
        f"Tryb:         {mode}\n"
        f"Request ID:   {request_id}\n"
        f"Otrzymano:    {received_at.isoformat(timespec='seconds')}\n"
        "\n"
        "----- Oryginalna wiadomość -----\n"
        f"{original_message}\n"
        "--------------------------------\n"
    )


def compose_routed_email(
    *,
    sender_email: str,
    department: Department,
    subject: str,
    reason: str,
    original_message: str,
    request_id: str,
    routed_by: RoutedBy,
    model_name: str,
    mail_settings: MailSettings,
    now: datetime | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """Create the e-mail forwarded to `department`.

    `now` and `message_id` can be injected to make tests deterministic.
    """
    received_at = now or datetime.now(UTC)
    clean_subject = sanitize_header(f"{mail_settings.subject_prefix} {subject}", max_length=160)

    message = EmailMessage()
    message["From"] = mail_settings.from_address
    message["To"] = department.value
    message["Reply-To"] = sender_email
    message["Subject"] = clean_subject
    message["Date"] = format_datetime(received_at)
    message["Message-ID"] = message_id or make_msgid(domain=mail_settings.message_id_domain)
    message["X-Request-ID"] = sanitize_header(request_id, max_length=64)
    message["X-AI-Router-Department"] = department.value
    message["X-AI-Router-Routed-By"] = routed_by
    message["X-AI-Router-Model"] = sanitize_header(model_name, max_length=100)
    message.set_content(
        _body(
            sender_email=sender_email,
            department=department,
            reason=sanitize_header(reason, max_length=300),
            original_message=original_message,
            request_id=request_id,
            routed_by=routed_by,
            received_at=received_at,
        )
    )
    return message
