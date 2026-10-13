"""HTTP request/response models (they also generate the OpenAPI / Swagger documentation)."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ai_router.config import get_settings
from ai_router.routing.departments import Department

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

    email: EmailStr = Field(description="Sender's e-mail address; used as `Reply-To`.")
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
        limit = get_settings().max_message_chars
        if len(cleaned) > limit:
            raise ValueError(f"message is too long ({len(cleaned)} > {limit} characters)")
        return cleaned


class EmailInfo(BaseModel):
    message_id: str = Field(description="`Message-ID` header of the forwarded e-mail.")
    subject: str
    to: Department
    reply_to: str


class Diagnostics(BaseModel):
    model: str = Field(description="LLM used by the agent.")
    llm_requests: int = Field(description="Number of requests sent to the model.")
    tool_calls: int = Field(description="Number of tool calls executed.")
    retries: int = Field(description="Number of retry prompts sent back to the model.")
    input_tokens: int
    output_tokens: int
    duration_ms: int


class RouteMessageResponse(BaseModel):
    request_id: str
    status: Literal["routed"] = "routed"
    routed_by: Literal["agent", "fallback"] = Field(
        description="`agent` = decided by the AI agent; `fallback` = agent failed, sent to other@."
    )
    department: Department
    reason: str = Field(description="Short justification of the routing decision.")
    email: EmailInfo
    diagnostics: Diagnostics
    warnings: list[str] = Field(default_factory=list)


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
