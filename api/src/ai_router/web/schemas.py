"""HTTP request/response models (they also generate the OpenAPI / Swagger documentation)."""

import re
from typing import Any, Literal

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Control characters except TAB (\x09) and LF (\x0a); CR is normalised to LF beforehand.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ROUTE_MESSAGE_EXAMPLES: dict[str, dict[str, Any]] = {
    "hardware": {
        "summary": "Broken computer (PL) -> help-desk",
        "value": {"email": "jan.nowak@example.com", "message": "Nie działa mi komputer"},
    },
    "leave": {
        "summary": "Leave request (PL) -> kadry",
        "value": {
            "email": "anna.kowalska@example.com",
            "message": "Chciałbym zgłosić urlop na jutro",
        },
    },
    "outage": {
        "summary": "Server outage (PL) -> it",
        "value": {
            "email": "piotr.zielinski@example.com",
            "message": "Serwer z bazą danych nie odpowiada, cały dział nie może pracować",
        },
    },
    "recruitment": {
        "summary": "Recruitment question (EN) -> human-resources",
        "value": {
            "email": "john.smith@example.com",
            "message": "Hi, I would like to recommend a friend for the open Java developer role.",
        },
    },
    "unclear": {
        "summary": "Unrelated question -> other",
        "value": {"email": "ewa.wisniewska@example.com", "message": "Gdzie mogę zaparkować rower?"},
    },
}


class RouteMessageRequest(BaseModel):
    """A message to be categorised and forwarded to the right department."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [ROUTE_MESSAGE_EXAMPLES["hardware"]["value"]]}
    )

    email: EmailStr = Field(
        description="Sender's e-mail address; used as `Reply-To`. With `AUTH_MODE=oidc` it must "
        "match the authenticated user."
    )
    message: str = Field(
        description=(
            "Free-form message text (any language). Control characters are removed; the "
            "maximum length is configured by `MAX_MESSAGE_CHARS` (default 4000)."
        ),
    )

    @field_validator("message")
    @classmethod
    def _clean_message(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("message must not contain NUL characters")
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = _CONTROL_CHARS.sub("", normalized).strip()
        if not cleaned:
            raise ValueError("message must not be empty")
        # The length limit depends on the app's settings (MAX_MESSAGE_CHARS), so it is checked by
        # `check_message_length` in the router and published in OpenAPI by the app factory.
        return cleaned


def check_message_length(message: str, limit: int) -> None:
    """422 in the same format as schema validation errors (`errors[].loc = ["body", "message"]`)."""
    if len(message) > limit:
        raise RequestValidationError(
            [
                {
                    "type": "string_too_long",
                    "loc": ("body", "message"),
                    "msg": f"message is too long ({len(message)} > {limit} characters)",
                    "input": None,
                }
            ]
        )


class EmailInfo(BaseModel):
    message_id: str = Field(description="`Message-ID` header of the forwarded e-mail.")
    subject: str
    to: str
    reply_to: str
    delivery: Literal["sent", "queued"] = Field(
        description="`sent` = accepted by the SMTP server; `queued` = stored in the outbox and "
        "retried in the background (e.g. SMTP temporarily unavailable)."
    )


class Diagnostics(BaseModel):
    model: str = Field(description="LLM used by the agent.")
    agent_mode: str = Field(description="`tool` or `output_function`.")
    prompt_version: str = Field(description="Hash of instructions, tool definition and mode.")
    llm_requests: int = Field(description="Number of requests sent to the model.")
    tool_calls: int = Field(description="Accepted `send_email` calls (0 or 1).")
    tool_duplicates: int = Field(description="Duplicate `send_email` calls blocked by the guard.")
    retries: int = Field(description="Retry prompts sent back to the model.")
    input_tokens: int
    output_tokens: int
    duration_ms: int
    queue_wait_ms: int = Field(description="Time spent waiting for a free agent slot.")


class RouteMessageResponse(BaseModel):
    ticket_id: str = Field(description="Server-generated ticket id (resource id).")
    ticket_code: str = Field(description="Human-friendly ticket number, e.g. `AIR-3F9A1C2E`.")
    request_id: str = Field(description="Correlation id (`X-Request-ID`).")
    status: Literal["routed"] = "routed"
    routed_by: Literal["agent", "fallback"] = Field(
        description="`agent` = decided by the AI agent; `fallback` = agent failed, sent to the "
        "fallback department for manual triage."
    )
    department: str = Field(description="Department the e-mail was sent to.")
    chosen_department: str = Field(
        description="Department chosen by the agent (differs only after a low-confidence redirect)."
    )
    reason: str = Field(description="Short justification of the routing decision.")
    confidence: float | None = Field(description="Model's self-reported certainty (0-1).")
    needs_review: bool = Field(description="True below `CONFIDENCE_THRESHOLD` or on fallback.")
    sender_verification: Literal["none", "client", "sso"]
    email: EmailInfo
    diagnostics: Diagnostics
    warnings: list[str] = Field(default_factory=list)


class TicketError(BaseModel):
    type: str
    title: str
    detail: str | None = None


class MessageStatusResponse(BaseModel):
    """Status of a ticket - returned with `202 Accepted` and by `GET /api/v1/messages/{id}`."""

    ticket_id: str
    ticket_code: str
    request_id: str
    status: Literal["queued", "processing", "routed", "failed"]
    mode: Literal["sync", "async"]
    created_at: str
    updated_at: str
    result: RouteMessageResponse | None = None
    error: TicketError | None = None
    links: dict[str, str]


class FeedbackRequest(BaseModel):
    correct_department: str = Field(description="Address of the department that should handle it.")
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    ticket_id: str
    verdict: Literal["confirmed", "corrected"]


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class Problem(BaseModel):
    """RFC 9457 Problem Details error body (`application/problem+json`)."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None
    errors: list[ValidationIssue] | None = None


class ComponentStatus(BaseModel):
    status: Literal["up", "down"]
    latency_ms: int | None = None
    name: str | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    components: dict[str, ComponentStatus]


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
