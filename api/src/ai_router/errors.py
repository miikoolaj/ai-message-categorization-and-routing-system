"""Application errors. Each one carries the HTTP status and RFC 9457 problem type it maps to.

They are raised by the domain/service layer and translated into responses in `web/errors.py`.
`detail` is shown to API clients, so it must never contain internal details (host names, ports,
exception text) - those belong in the logs.
"""

from typing import ClassVar


class AppError(Exception):
    """Base class of errors that map to a specific HTTP status and problem type."""

    status: ClassVar[int] = 500
    type_suffix: ClassVar[str] = "internal-error"
    title: ClassVar[str] = "Internal server error"
    default_retry_after: ClassVar[int | None] = None

    def __init__(self, detail: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.retry_after = retry_after if retry_after is not None else self.default_retry_after


class AuthenticationError(AppError):
    status, type_suffix, title = 401, "unauthenticated", "Authentication required"


class PermissionDeniedError(AppError):
    status, type_suffix, title = 403, "forbidden", "Access denied"


class TicketNotFoundError(AppError):
    status, type_suffix, title = 404, "ticket-not-found", "Ticket not found"


class IdempotencyInProgressError(AppError):
    status, type_suffix, title = 409, "idempotency-in-progress", "Request still in progress"
    default_retry_after = 5


class IdempotencyConflictError(AppError):
    status, type_suffix, title = 422, "idempotency-conflict", "Idempotency key reused"


class TooManyRequestsError(AppError):
    status, type_suffix, title = 429, "too-many-requests", "Too many requests"
    default_retry_after = 10


class AgentFailedError(AppError):
    status, type_suffix, title = 502, "agent-failed", "The AI agent failed to route the message"


class LlmUnavailableError(AppError):
    status, type_suffix, title = 503, "llm-unavailable", "LLM backend unavailable"
    default_retry_after = 30


class LlmMisconfiguredError(AppError):
    status, type_suffix, title = 503, "llm-misconfigured", "LLM backend misconfigured"
    default_retry_after = 60


class StorageUnavailableError(AppError):
    status, type_suffix, title = 503, "storage-unavailable", "Message storage unavailable"
    default_retry_after = 30


class RoutingTimeoutError(AppError):
    status, type_suffix, title = 504, "routing-timeout", "Routing timed out"
