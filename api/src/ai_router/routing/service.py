"""Orchestration of a single routing request.

Responsibilities (business process, not AI details): concurrency limit, overall timeout, verifying
the outcome (the e-mail actually sent by the tool is the source of truth) and the technical
fallback to `other@example.com` when the agent fails.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolReturnPart,
    UsageLimits,
    capture_run_messages,
)
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded

from ai_router.config import Settings
from ai_router.errors import (
    AgentFailedError,
    AppError,
    LlmUnavailableError,
    RoutingTimeoutError,
    SmtpUnavailableError,
    TooManyRequestsError,
)
from ai_router.mail.composer import MailSettings, compose_routed_email, sanitize_header
from ai_router.mail.sender import EmailDeliveryError, EmailSender
from ai_router.routing.departments import Department
from ai_router.routing.deps import RoutingDeps, SentEmail
from ai_router.routing.prompts import build_user_prompt

logger = logging.getLogger(__name__)

# Reasons why the agent did not send the e-mail itself.
FAILURE_TIMEOUT = "timeout"
FAILURE_USAGE_LIMIT = "usage_limit"
FAILURE_MODEL_BEHAVIOR = "model_behavior"
FAILURE_LLM_UNAVAILABLE = "llm_unavailable"
FAILURE_NO_TOOL_CALL = "no_tool_call"


@dataclass(frozen=True, slots=True)
class AgentDiagnostics:
    model: str
    llm_requests: int
    tool_calls: int
    retries: int
    input_tokens: int
    output_tokens: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    request_id: str
    sent: SentEmail
    diagnostics: AgentDiagnostics
    warnings: tuple[str, ...] = ()


def summarize_messages(messages: Sequence[ModelMessage]) -> tuple[int, int, int, int, int]:
    """(llm_requests, tool_calls, retries, input_tokens, output_tokens) of an agent run."""
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    request_parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    return (
        len(responses),
        sum(isinstance(p, ToolReturnPart) for p in request_parts),
        sum(isinstance(p, RetryPromptPart) for p in request_parts),
        sum(r.usage.input_tokens for r in responses),
        sum(r.usage.output_tokens for r in responses),
    )


class RoutingService:
    def __init__(
        self,
        *,
        agent: Agent[RoutingDeps, str],
        mailer: EmailSender,
        settings: Settings,
        model_name: str,
    ) -> None:
        self._agent = agent
        self._mailer = mailer
        self._settings = settings
        self._model_name = model_name
        self._mail_settings = MailSettings.from_settings(settings)
        self._slots = asyncio.Semaphore(settings.max_concurrent_agent_runs)

    async def route(self, *, sender_email: str, message: str, request_id: str) -> RoutingOutcome:
        try:
            await asyncio.wait_for(
                self._slots.acquire(), timeout=self._settings.agent_queue_timeout_seconds
            )
        except TimeoutError:
            raise TooManyRequestsError(
                "All agent slots are busy; retry later.",
                retry_after=int(self._settings.agent_queue_timeout_seconds),
            ) from None
        try:
            return await self._route(
                sender_email=sender_email, message=message, request_id=request_id
            )
        finally:
            self._slots.release()

    async def _route(self, *, sender_email: str, message: str, request_id: str) -> RoutingOutcome:
        self._log_received(sender_email, message)
        deps = RoutingDeps(
            request_id=request_id,
            sender_email=sender_email,
            original_message=message,
            mailer=self._mailer,
            mail_settings=self._mail_settings,
            model_name=self._model_name,
        )
        started = time.perf_counter()
        failure: str | None = None

        with capture_run_messages() as messages:
            try:
                async with asyncio.timeout(self._settings.agent_timeout_seconds):
                    await self._agent.run(
                        build_user_prompt(message),
                        deps=deps,
                        # Duplicate tool calls are neutralised by the guard in the tool, so only
                        # the number of model requests needs a hard cap.
                        usage_limits=UsageLimits(request_limit=self._settings.agent_request_limit),
                    )
            except EmailDeliveryError as exc:
                raise SmtpUnavailableError(f"Could not deliver the e-mail: {exc}") from exc
            except TimeoutError:
                failure = FAILURE_TIMEOUT
            except UsageLimitExceeded:
                failure = FAILURE_USAGE_LIMIT
            except UnexpectedModelBehavior:
                failure = FAILURE_MODEL_BEHAVIOR
            except ModelAPIError as exc:
                logger.warning("llm.error", extra={"error": repr(exc)})
                failure = FAILURE_LLM_UNAVAILABLE

        diagnostics = self._diagnostics(messages, started)
        if deps.sent is not None:
            warnings = (f"post_send_{failure}",) if failure else ()
            return self._completed(request_id, deps.sent, diagnostics, warnings)

        failure = failure or FAILURE_NO_TOOL_CALL
        logger.warning("routing.agent_failed", extra={"cause": failure, **_as_log(diagnostics)})
        if not self._settings.routing_fallback_enabled:
            raise self._failure_error(failure)
        sent = await self._send_fallback(deps, failure)
        return self._completed(request_id, sent, diagnostics, (f"agent_failed: {failure}",))

    async def _send_fallback(self, deps: RoutingDeps, failure: str) -> SentEmail:
        reason = (
            f"Automatyczne przypisanie działu nie powiodło się ({failure}). "
            "Zgłoszenie wymaga ręcznej weryfikacji."
        )
        subject = sanitize_header(f"Do weryfikacji: {deps.original_message}", max_length=80)
        email = compose_routed_email(
            sender_email=deps.sender_email,
            department=Department.OTHER,
            subject=subject,
            reason=reason,
            original_message=deps.original_message,
            request_id=deps.request_id,
            routed_by="fallback",
            model_name=deps.model_name,
            mail_settings=deps.mail_settings,
        )
        try:
            await self._mailer.send(email)
        except EmailDeliveryError as exc:
            raise SmtpUnavailableError(f"Could not deliver the fallback e-mail: {exc}") from exc
        logger.warning(
            "routing.fallback", extra={"cause": failure, "message_id": email["Message-ID"]}
        )
        return SentEmail(
            department=Department.OTHER,
            to=Department.OTHER.value,
            reply_to=deps.sender_email,
            subject=str(email["Subject"]),
            reason=reason,
            message_id=str(email["Message-ID"]),
            routed_by="fallback",
        )

    def _completed(
        self,
        request_id: str,
        sent: SentEmail,
        diagnostics: AgentDiagnostics,
        warnings: tuple[str, ...],
    ) -> RoutingOutcome:
        logger.info(
            "routing.completed",
            extra={
                "department": sent.department.value,
                "routed_by": sent.routed_by,
                "message_id": sent.message_id,
                "warnings": list(warnings),
                **_as_log(diagnostics),
            },
        )
        return RoutingOutcome(request_id, sent, diagnostics, warnings)

    def _diagnostics(self, messages: Sequence[ModelMessage], started: float) -> AgentDiagnostics:
        llm_requests, tool_calls, retries, input_tokens, output_tokens = summarize_messages(
            messages
        )
        return AgentDiagnostics(
            model=self._model_name,
            llm_requests=llm_requests,
            tool_calls=tool_calls,
            retries=retries,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _failure_error(failure: str) -> AppError:
        if failure == FAILURE_LLM_UNAVAILABLE:
            return LlmUnavailableError("The LLM backend did not respond and fallback is disabled.")
        if failure == FAILURE_TIMEOUT:
            return RoutingTimeoutError("The agent did not finish in time and fallback is disabled.")
        return AgentFailedError(
            f"The agent did not send the e-mail ({failure}); fallback is disabled."
        )

    def _log_received(self, sender_email: str, message: str) -> None:
        extra: dict[str, object] = {
            "message_chars": len(message),
            "message_sha256": hashlib.sha256(message.encode()).hexdigest()[:16],
            "sender_domain": sender_email.rsplit("@", 1)[-1],
        }
        if self._settings.log_message_content:
            extra["message_text"] = message
        logger.info("routing.started", extra=extra)


def _as_log(diagnostics: AgentDiagnostics) -> dict[str, object]:
    return {
        "llm_requests": diagnostics.llm_requests,
        "tool_calls": diagnostics.tool_calls,
        "retries": diagnostics.retries,
        "input_tokens": diagnostics.input_tokens,
        "output_tokens": diagnostics.output_tokens,
        "duration_ms": diagnostics.duration_ms,
    }
