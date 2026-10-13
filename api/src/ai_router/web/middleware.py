"""Pure ASGI middleware: request-ID correlation, request logging, body size limit and a
last-resort error handler (so even a 500 carries the request ID)."""

import logging
import re
import time
import uuid

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_router.logging_config import request_id_var
from ai_router.web.errors import problem_response

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
HEALTH_PATH_PREFIX = "/api/v1/health/"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int = 64 * 1024) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming = headers.get(REQUEST_ID_HEADER)
        valid = incoming is not None and _VALID_REQUEST_ID.fullmatch(incoming)
        request_id = incoming if valid and incoming else str(uuid.uuid4())
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False
        received_bytes = 0

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        async def receive_with_limit() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    # fastapi.HTTPException passes through FastAPI's body parsing unchanged.
                    raise HTTPException(413, f"Request body exceeds {self.max_body_bytes} bytes.")
            return message

        try:
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self.max_body_bytes:
                response = problem_response(
                    status=413,
                    type_suffix="payload-too-large",
                    title="Payload too large",
                    detail=f"Request body exceeds {self.max_body_bytes} bytes.",
                    instance=scope["path"],
                )
                await response(scope, receive, send_with_request_id)
                return
            await self.app(scope, receive_with_limit, send_with_request_id)
        except Exception:
            logger.exception("http.unhandled_error", extra={"path": scope["path"]})
            if not response_started:
                response = problem_response(
                    status=500,
                    type_suffix="internal-error",
                    title="Internal server error",
                    instance=scope["path"],
                )
                await response(scope, receive, send_with_request_id)
        finally:
            # Health probes run every few seconds - keep them out of INFO logs.
            level = logging.DEBUG if scope["path"].startswith(HEALTH_PATH_PREFIX) else logging.INFO
            logger.log(
                level,
                "http.request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            request_id_var.reset(token)
