"""Building the routed e-mail. Pure functions only - no I/O, no AI.

Security-relevant values are set here by code, never by the LLM: `Reply-To` (sender's address from
the request or identity), the original message (verbatim), `From`, `Message-ID`, the ticket number,
the sender-verification banner and the diagnostic `X-AI-Router-*` headers.
"""

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ai_router.config import Settings

RoutedBy = Literal["agent", "fallback"]
Verification = Literal["none", "client", "sso"]

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")

VERIFICATION_BANNERS: dict[Verification, str] = {
    "none": (
        "UWAGA: adres nadawcy NIE został zweryfikowany (API działa bez uwierzytelniania). "
        "Nie realizuj próśb dotyczących haseł, płatności ani danych osobowych bez potwierdzenia "
        "tożsamości innym kanałem."
    ),
    "client": (
        "Adres nadawcy przekazał uwierzytelniony system kliencki; tożsamości osoby nie "
        "potwierdzono przez SSO."
    ),
    "sso": "Nadawca zweryfikowany przez firmowe SSO.",
}

AI_NOTE = "Temat i uzasadnienie wygenerował automatycznie model językowy."


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


@dataclass(frozen=True, slots=True)
class _Fields:
    """Everything shown in the body - rendered as plain text and as HTML."""

    ticket_code: str
    sender_email: str
    banner: str
    department: str
    reason: str
    confidence_line: str | None
    mode: str
    request_id: str
    received_at: str
    original_message: str
    feedback_url: str | None


def _text_body(f: _Fields) -> str:
    lines = [
        "Nowe zgłoszenie przekazane automatycznie przez AI Router.",
        "",
        f"Zgłoszenie:   {f.ticket_code}",
        f"Nadawca:      {f.sender_email}  (odpowiedz na tę wiadomość, aby odpisać nadawcy)",
        f"              {f.banner}",
        f"Dział:        {f.department}",
        f"Uzasadnienie: {f.reason}",
    ]
    if f.confidence_line:
        lines.append(f"Pewność:      {f.confidence_line}")
    lines += [
        f"Tryb:         {f.mode}",
        f"Request ID:   {f.request_id}",
        f"Otrzymano:    {f.received_at}",
        "",
        AI_NOTE,
        "",
        "----- Oryginalna wiadomość -----",
        f.original_message,
        "--------------------------------",
    ]
    if f.feedback_url:
        lines += ["", f"Zły dział? Wskaż właściwy: {f.feedback_url}"]
    return "\n".join(lines) + "\n"


def _html_body(f: _Fields) -> str:
    e = html.escape
    rows = [
        ("Zgłoszenie", e(f.ticket_code)),
        ("Nadawca", f"{e(f.sender_email)}<br><small>{e(f.banner)}</small>"),
        ("Dział", e(f.department)),
        ("Uzasadnienie", e(f.reason)),
    ]
    if f.confidence_line:
        rows.append(("Pewność", e(f.confidence_line)))
    rows += [("Tryb", e(f.mode)), ("Request ID", e(f.request_id)), ("Otrzymano", e(f.received_at))]
    table = "".join(
        f'<tr><th style="text-align:left;padding:2px 12px 2px 0">{k}</th><td>{v}</td></tr>'
        for k, v in rows
    )
    feedback = (
        f'<p><a href="{e(f.feedback_url)}">Zły dział? Wskaż właściwy</a></p>'
        if f.feedback_url
        else ""
    )
    return (
        '<html><body style="font-family:sans-serif">'
        "<p>Nowe zgłoszenie przekazane automatycznie przez AI Router.</p>"
        f"<table>{table}</table>"
        f"<p><small>{e(AI_NOTE)}</small></p>"
        "<p><strong>Oryginalna wiadomość:</strong></p>"
        f'<pre style="white-space:pre-wrap;background:#f4f4f4;padding:8px">'
        f"{e(f.original_message)}</pre>"
        f"{feedback}</body></html>"
    )


def compose_routed_email(
    *,
    sender_email: str,
    department: str,
    subject: str,
    reason: str,
    original_message: str,
    request_id: str,
    ticket_code: str,
    routed_by: RoutedBy,
    model_name: str,
    prompt_version: str,
    mail_settings: MailSettings,
    sender_verification: Verification = "none",
    confidence: float | None = None,
    needs_review: bool = False,
    feedback_url: str | None = None,
    now: datetime | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """Create the e-mail forwarded to `department` (plain text + HTML alternative).

    `now` and `message_id` can be injected to make tests deterministic.
    """
    received_at = now or datetime.now(UTC)
    review = "[Do weryfikacji] " if needs_review else ""
    clean_subject = sanitize_header(
        f"{mail_settings.subject_prefix} [{ticket_code}] {review}{subject}", max_length=200
    )
    confidence_line = None
    if confidence is not None:
        confidence_line = f"{confidence:.2f}"
        if needs_review:
            confidence_line += " (poniżej progu - decyzja wymaga weryfikacji)"
    mode = (
        "agent AI (automatyczne przypisanie)"
        if routed_by == "agent"
        else "fallback (automatyczne przypisanie nie powiodło się - wymagana weryfikacja)"
    )
    fields = _Fields(
        ticket_code=ticket_code,
        sender_email=sender_email,
        banner=VERIFICATION_BANNERS[sender_verification],
        department=department,
        reason=sanitize_header(reason, max_length=300),
        confidence_line=confidence_line,
        mode=mode,
        request_id=request_id,
        received_at=received_at.isoformat(timespec="seconds"),
        original_message=original_message,
        feedback_url=feedback_url,
    )

    message = EmailMessage()
    message["From"] = mail_settings.from_address
    message["To"] = department
    message["Reply-To"] = sender_email
    message["Subject"] = clean_subject
    message["Date"] = format_datetime(received_at)
    message["Message-ID"] = message_id or make_msgid(domain=mail_settings.message_id_domain)
    message["X-Request-ID"] = sanitize_header(request_id, max_length=64)
    message["X-AI-Router-Ticket"] = ticket_code
    message["X-AI-Router-Department"] = department
    message["X-AI-Router-Routed-By"] = routed_by
    message["X-AI-Router-Model"] = sanitize_header(model_name, max_length=100)
    message["X-AI-Router-Prompt-Version"] = prompt_version
    message["X-AI-Router-Sender-Verification"] = sender_verification
    message["X-AI-Router-Needs-Review"] = "true" if needs_review else "false"
    if confidence is not None:
        message["X-AI-Router-Confidence"] = f"{confidence:.2f}"
    message.set_content(_text_body(fields))
    message.add_alternative(_html_body(fields), subtype="html")
    return message
