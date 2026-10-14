"""Translation of errors into RFC 9457 Problem Details responses (`application/problem+json`)."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_router.errors import AppError
from ai_router.logging_config import current_request_id, log_event

logger = logging.getLogger(__name__)

PROBLEM_TYPE_BASE = "https://ai-router.local/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"


def problem_response(
    *,
    status: int,
    type_suffix: str,
    title: str,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + type_suffix,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": current_request_id(),
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(
        {k: v for k, v in body.items() if v is not None},
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - registration guarantees the type
        raise exc
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    level = logging.WARNING if exc.status < 500 else logging.ERROR
    log_event(logger, level, "http.app_error", status=exc.status, error_type=exc.type_suffix)
    return problem_response(
        status=exc.status,
        type_suffix=exc.type_suffix,
        title=exc.title,
        detail=exc.detail,
        instance=request.url.path,
        headers=headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return problem_response(
        status=422,
        type_suffix="validation-error",
        title="Request validation failed",
        detail="The request body does not match the expected schema.",
        instance=request.url.path,
        errors=jsonable_encoder(errors),
    )


_HTTP_PROBLEMS = {
    400: ("bad-request", "Bad request"),
    404: ("not-found", "Not found"),
    405: ("method-not-allowed", "Method not allowed"),
    413: ("payload-too-large", "Payload too large"),
}


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    type_suffix, title = _HTTP_PROBLEMS.get(exc.status_code, ("http-error", "HTTP error"))
    return problem_response(
        status=exc.status_code,
        type_suffix=type_suffix,
        title=title,
        detail=str(exc.detail) if exc.detail else None,
        instance=request.url.path,
        headers=dict(exc.headers) if exc.headers else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
