"""`POST /api/v1/messages` - the routing endpoint."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Response

from ai_router.logging_config import current_request_id
from ai_router.routing.service import RoutingOutcome
from ai_router.web.dependencies import ContainerDep
from ai_router.web.idempotency import IdempotencyStore
from ai_router.web.schemas import (
    ROUTE_MESSAGE_EXAMPLES,
    Diagnostics,
    EmailInfo,
    Problem,
    RouteMessageRequest,
    RouteMessageResponse,
)

router = APIRouter(prefix="/api/v1", tags=["messages"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    status: {
        "model": Problem,
        "description": description,
        "content": {"application/problem+json": {}},
    }
    for status, description in {
        413: "Request body too large",
        422: "Invalid input (e.g. malformed e-mail, empty or too long message)",
        429: "All agent slots busy - retry after `Retry-After` seconds",
        502: "The agent failed and fallback routing is disabled",
        503: "LLM or SMTP unavailable (and fallback impossible or disabled)",
        504: "Routing timed out and fallback routing is disabled",
    }.items()
}


def to_response(outcome: RoutingOutcome) -> RouteMessageResponse:
    sent, diag = outcome.sent, outcome.diagnostics
    return RouteMessageResponse(
        request_id=outcome.request_id,
        routed_by=sent.routed_by,
        department=sent.department,
        reason=sent.reason,
        email=EmailInfo(
            message_id=sent.message_id,
            subject=sent.subject,
            to=sent.department,
            reply_to=sent.reply_to,
        ),
        diagnostics=Diagnostics(
            model=diag.model,
            llm_requests=diag.llm_requests,
            tool_calls=diag.tool_calls,
            retries=diag.retries,
            input_tokens=diag.input_tokens,
            output_tokens=diag.output_tokens,
            duration_ms=diag.duration_ms,
        ),
        warnings=list(outcome.warnings),
    )


@router.post(
    "/messages",
    summary="Categorise a message and forward it to the right department",
    description=(
        "The AI agent (local LLM via Ollama) reads the message, chooses one of the departments "
        "and forwards the message by calling its `send_email` tool. The e-mail has `Reply-To` set "
        "to the sender's address and can be inspected in Mailpit. On CPU a request usually takes "
        "5-20 seconds."
    ),
    responses=_PROBLEMS,
)
async def route_message(
    payload: Annotated[RouteMessageRequest, Body(openapi_examples=ROUTE_MESSAGE_EXAMPLES)],
    container: ContainerDep,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=128,
            description=(
                "Optional. Repeating a request with the same key does not send a second e-mail."
            ),
        ),
    ] = None,
) -> RouteMessageResponse:
    request_id = current_request_id() or str(uuid.uuid4())

    async def operation() -> RouteMessageResponse:
        outcome = await container.routing_service.route(
            sender_email=str(payload.email), message=payload.message, request_id=request_id
        )
        return to_response(outcome)

    if idempotency_key is None:
        return await operation()
    fingerprint = IdempotencyStore.fingerprint(payload.model_dump(mode="json"))
    result, replayed = await container.idempotency.execute(idempotency_key, fingerprint, operation)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result
