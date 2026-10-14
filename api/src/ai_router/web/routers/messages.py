"""`POST /api/v1/messages` (sync or async) and `GET /api/v1/messages/{ticket_id}` (status)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Path
from fastapi.responses import JSONResponse

from ai_router.errors import TicketNotFoundError
from ai_router.logging_config import current_request_id
from ai_router.routing.tickets import Submission
from ai_router.storage.tickets import TicketRecord
from ai_router.web.dependencies import AuthDep, ContainerDep
from ai_router.web.idempotency import StoredResponse, fingerprint, run_idempotent
from ai_router.web.schemas import (
    ROUTE_MESSAGE_EXAMPLES,
    MessageStatusResponse,
    Problem,
    RouteMessageRequest,
    RouteMessageResponse,
    TicketError,
    check_message_length,
)

router = APIRouter(prefix="/api/v1", tags=["messages"])

TicketId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="Ticket id (32 hex chars).")]

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    status: {
        "model": Problem,
        "description": description,
        "content": {"application/problem+json": {}},
    }
    for status, description in {
        401: "Authentication required (AUTH_MODE=api_key or oidc)",
        403: "Forbidden (e.g. `email` does not match the SSO user)",
        404: "Unknown ticket",
        409: "A request with the same Idempotency-Key is still in progress",
        413: "Request body too large",
        422: "Invalid input or Idempotency-Key reused with a different body",
        429: "Rate limit exceeded or all agent slots busy - retry after `Retry-After` seconds",
        502: "The agent failed and fallback routing is disabled",
        503: "LLM or storage unavailable (and fallback impossible or disabled)",
        504: "Routing timed out and fallback routing is disabled",
    }.items()
}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def status_response(ticket: TicketRecord) -> MessageStatusResponse:
    return MessageStatusResponse(
        ticket_id=ticket.ticket_id,
        ticket_code=ticket.ticket_code,
        request_id=ticket.request_id,
        status=ticket.status,
        mode=ticket.mode,
        created_at=_iso(ticket.created_at),
        updated_at=_iso(ticket.updated_at),
        result=RouteMessageResponse.model_validate(ticket.result) if ticket.result else None,
        error=TicketError.model_validate(ticket.error) if ticket.error else None,
        links={"self": f"/api/v1/messages/{ticket.ticket_id}"},
    )


@router.post(
    "/messages",
    summary="Categorise a message and forward it to the right department",
    description=(
        "The AI agent (local LLM via Ollama) reads the message, chooses one of the departments "
        "and forwards it by calling its `send_email` tool. The e-mail has `Reply-To` set to the "
        "sender and is delivered through a durable outbox (inspect it in Mailpit).\n\n"
        "* **Synchronous** (default): `200` with the routing result; on CPU usually 5-20 s.\n"
        "* **Asynchronous**: header `Prefer: respond-async` returns `202` immediately with a "
        "ticket; poll `GET /api/v1/messages/{ticket_id}` (link in `Location`)."
    ),
    response_model=RouteMessageResponse,
    responses={
        202: {"model": MessageStatusResponse, "description": "Accepted (asynchronous mode)"},
        **{k: v for k, v in _PROBLEMS.items() if k != 404},
    },
)
async def route_message(
    payload: Annotated[RouteMessageRequest, Body(openapi_examples=ROUTE_MESSAGE_EXAMPLES)],
    container: ContainerDep,
    auth: AuthDep,
    prefer: Annotated[
        str | None,
        Header(description="`respond-async` = return 202 with a ticket and route in background."),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=128,
            description="Optional. Repeating a request with the same key returns the stored "
            "response instead of sending a second e-mail.",
        ),
    ] = None,
) -> JSONResponse:
    container.rate_limiter.check(auth.rate_key)
    check_message_length(payload.message, container.settings.max_message_chars)
    sender = auth.sender_for(str(payload.email))
    async_mode = prefer is not None and "respond-async" in prefer.lower()
    submission = Submission(
        request_id=current_request_id() or str(uuid.uuid4()),
        client_id=auth.client_id,
        sender_email=sender,
        sender_verification=auth.sender_verification,
        message=payload.message,
    )

    async def operation() -> StoredResponse:
        if async_mode:
            ticket = await container.tickets.submit_async(submission)
            body = status_response(ticket).model_dump(mode="json")
            return StoredResponse(202, body, {"Location": body["links"]["self"]})
        outcome = await container.tickets.route_sync(submission)
        result = RouteMessageResponse.model_validate(outcome.to_dict())
        return StoredResponse(200, result.model_dump(mode="json"))

    replayed = False
    if idempotency_key is None:
        stored = await operation()
    else:
        stored, replayed = await run_idempotent(
            container.idempotency,
            scope_key=f"{auth.client_id}:{idempotency_key}",
            payload_fingerprint=fingerprint(
                {**payload.model_dump(mode="json"), "async": async_mode}
            ),
            operation=operation,
            wait_timeout=container.settings.agent_timeout_seconds + 30,
            stale_after=container.settings.agent_timeout_seconds + 60,
        )
    headers = dict(stored.headers)
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(stored.body, status_code=stored.status_code, headers=headers)


@router.get(
    "/messages/{ticket_id}",
    summary="Status and result of a ticket (asynchronous mode, audit)",
    response_model=MessageStatusResponse,
    responses={401: _PROBLEMS[401], 404: _PROBLEMS[404]},
)
async def get_message(
    ticket_id: TicketId, container: ContainerDep, auth: AuthDep
) -> MessageStatusResponse:
    ticket = await container.tickets.get(ticket_id)
    # With authentication enabled, other clients' tickets look exactly like missing ones.
    if ticket is None or (
        container.settings.auth_mode != "none" and ticket.client_id != auth.client_id
    ):
        raise TicketNotFoundError()
    return status_response(ticket)
