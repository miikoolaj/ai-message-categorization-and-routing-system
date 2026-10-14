"""Orchestration of a single routing request.

Responsibilities (business process, not AI details): concurrency limit, overall timeout, verifying
the outcome and the technical fallback. The source of truth is the record accepted by the mail
gateway (outbox) - never the text of the model, and never a flag that a cancelled task may have
left behind. This is what makes "exactly one e-mail per request" hold even on timeouts.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    UsageLimits,
    capture_run_messages,
)
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from ai_router.config import Settings
from ai_router.errors import (
    AgentFailedError,
    AppError,
    LlmMisconfiguredError,
    LlmUnavailableError,
    RoutingTimeoutError,
    StorageUnavailableError,
    TooManyRequestsError,
)
from ai_router.logging_config import log_event
from ai_router.mail.composer import compose_routed_email, sanitize_header
from ai_router.mail.gateway import MailGateway, MailGatewayError
from ai_router.observability import metrics
from ai_router.routing.agent import RoutingAgent
from ai_router.routing.deps import AgentRuntime, RoutingDeps, SenderVerification, SentEmail
from ai_router.routing.prompts import build_user_prompt

logger = logging.getLogger(__name__)

# Reasons why the agent did not hand the e-mail over itself.
FAILURE_TIMEOUT = "timeout"
FAILURE_USAGE_LIMIT = "usage_limit"
FAILURE_MODEL_BEHAVIOR = "model_behavior"
FAILURE_LLM_UNAVAILABLE = "llm_unavailable"
FAILURE_LLM_MISCONFIGURED = "llm_misconfigured"
FAILURE_NO_TOOL_CALL = "no_tool_call"


def classify_http_error(exc: ModelHTTPError) -> str:
    """Tell a broken model response apart from a misconfiguration or an outage."""
    body = str(exc.body or "").lower()
    if exc.status_code >= 500 and "tool call" in body:
        return FAILURE_MODEL_BEHAVIOR  # e.g. looping model / truncated tool-call JSON
    if exc.status_code in (400, 404):
        return FAILURE_LLM_MISCONFIGURED  # missing model, model without tool support
    return FAILURE_LLM_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    ticket_id: str
    ticket_code: str
    request_id: str
    sender_email: str
    sender_verification: SenderVerification
    message: str
    feedback_url: str | None = None


@dataclass(frozen=True, slots=True)
class AgentDiagnostics:
    model: str
    agent_mode: str
    prompt_version: str
    llm_requests: int
    tool_calls: int
    tool_duplicates: int
    retries: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    queue_wait_ms: int


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    request: RoutingRequest
    sent: SentEmail
    diagnostics: AgentDiagnostics
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """The routing result as returned by the API and stored with the ticket."""
        sent = self.sent
        return {
            "ticket_id": self.request.ticket_id,
            "ticket_code": self.request.ticket_code,
            "request_id": self.request.request_id,
            "status": "routed",
            "routed_by": sent.routed_by,
            "department": sent.department,
            "chosen_department": sent.chosen_department,
            "reason": sent.reason,
            "confidence": sent.confidence,
            "needs_review": sent.needs_review,
            "sender_verification": self.request.sender_verification,
            "email": {
                "message_id": sent.message_id,
                "subject": sent.subject,
                "to": sent.department,
                "reply_to": sent.reply_to,
                "delivery": sent.delivery,
            },
            "diagnostics": asdict(self.diagnostics),
            "warnings": list(self.warnings),
        }


def summarize_messages(messages: Sequence[ModelMessage]) -> tuple[int, int, int, int, int]:
    """(llm_requests, retries, input_tokens, output_tokens, max_input_tokens) of an agent run."""
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    request_parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    return (
        len(responses),
        sum(isinstance(p, RetryPromptPart) for p in request_parts),
        sum(r.usage.input_tokens for r in responses),
        sum(r.usage.output_tokens for r in responses),
        max((r.usage.input_tokens for r in responses), default=0),
    )


class RoutingService:
    def __init__(
        self,
        *,
        routing_agent: RoutingAgent,
        gateway: MailGateway,
        runtime: AgentRuntime,
        settings: Settings,
    ) -> None:
        self._agent = routing_agent
        self._gateway = gateway
        self._runtime = runtime
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent_agent_runs)
        self.inflight = 0

    @property
    def prompt_version(self) -> str:
        return self._agent.prompt_version

    async def route(
        self, request: RoutingRequest, *, wait_for_slot: bool = False
    ) -> RoutingOutcome:
        """Route one message. `wait_for_slot=True` (async worker) waits without a queue timeout."""
        waiting_since = time.perf_counter()
        if wait_for_slot:
            await self._slots.acquire()
        else:
            try:
                await asyncio.wait_for(
                    self._slots.acquire(), timeout=self._settings.agent_queue_timeout_seconds
                )
            except TimeoutError:
                metrics.REJECTED.labels("overloaded").inc()
                raise TooManyRequestsError(
                    "All agent slots are busy; retry later or use asynchronous mode "
                    "(header `Prefer: respond-async`).",
                    retry_after=int(self._settings.agent_queue_timeout_seconds),
                ) from None
        queue_wait = time.perf_counter() - waiting_since
        metrics.QUEUE_WAIT.observe(queue_wait)
        self.inflight += 1
        try:
            return await self._route(request, queue_wait_ms=int(queue_wait * 1000))
        finally:
            self.inflight -= 1
            self._slots.release()

    async def _route(self, request: RoutingRequest, *, queue_wait_ms: int) -> RoutingOutcome:
        self._log_received(request)
        deps = RoutingDeps(
            runtime=self._runtime,
            gateway=self._gateway,
            ticket_id=request.ticket_id,
            ticket_code=request.ticket_code,
            request_id=request.request_id,
            sender_email=request.sender_email,
            sender_verification=request.sender_verification,
            original_message=request.message,
            feedback_url=request.feedback_url,
        )
        started = time.perf_counter()
        failure: str | None = None

        with capture_run_messages() as messages:
            try:
                async with asyncio.timeout(self._settings.agent_timeout_seconds):
                    await self._agent.agent.run(
                        build_user_prompt(request.message),
                        deps=deps,
                        # Duplicate tool calls are neutralised by the guard in the tool, so only
                        # the number of model requests needs a hard cap.
                        usage_limits=UsageLimits(request_limit=self._settings.agent_request_limit),
                    )
            except MailGatewayError as exc:
                log_event(logger, logging.ERROR, "routing.storage_error", error=repr(exc))
                raise StorageUnavailableError() from exc
            except TimeoutError:
                failure = FAILURE_TIMEOUT
            except UsageLimitExceeded:
                failure = FAILURE_USAGE_LIMIT
            except UnexpectedModelBehavior:
                failure = FAILURE_MODEL_BEHAVIOR
            except ModelHTTPError as exc:  # subclass of ModelAPIError - must come first
                failure = classify_http_error(exc)
                log_event(logger, logging.WARNING, "llm.http_error", status=exc.status_code)
            except ModelAPIError as exc:
                failure = FAILURE_LLM_UNAVAILABLE
                log_event(logger, logging.WARNING, "llm.error", error=repr(exc))

        sent = await self._resolve_sent(deps)
        diagnostics = self._diagnostics(messages, deps, started, queue_wait_ms)
        if sent is not None:
            warnings: list[str] = [f"post_send_{failure}"] if failure else []
            if sent.delivery == "queued":
                warnings.append("delivery_queued")
            if sent.needs_review:
                warnings.append("low_confidence")
            return self._completed(request, sent, diagnostics, tuple(warnings))

        failure = failure or FAILURE_NO_TOOL_CALL
        metrics.FALLBACKS.labels(failure).inc()
        log_event(
            logger, logging.WARNING, "routing.agent_failed", cause=failure, **asdict(diagnostics)
        )
        if not self._settings.routing_fallback_enabled:
            raise self._failure_error(failure)
        sent = await self._send_fallback(deps, failure)
        warnings = [f"agent_failed: {failure}"]
        if sent.delivery == "queued":
            warnings.append("delivery_queued")
        return self._completed(request, sent, diagnostics, tuple(warnings))

    async def _resolve_sent(self, deps: RoutingDeps) -> SentEmail | None:
        """What was really handed over - also when the tool was interrupted by a timeout."""
        if deps.sent is not None:
            return deps.sent
        if deps.submission is not None and not deps.submission.done():
            # The hand-over continues in the background (shielded); give it time to finish.
            grace = self._settings.mail_sync_delivery_timeout_seconds + 2
            await asyncio.wait({deps.submission}, timeout=grace)
        try:
            return await self._gateway.find(deps.ticket_id)
        except MailGatewayError as exc:
            raise StorageUnavailableError() from exc

    async def _send_fallback(self, deps: RoutingDeps, failure: str) -> SentEmail:
        fallback = self._runtime.policy.fallback
        reason = (
            f"Automatyczne przypisanie działu nie powiodło się ({failure}). "
            "Zgłoszenie wymaga ręcznej weryfikacji."
        )
        email = compose_routed_email(
            sender_email=deps.sender_email,
            sender_verification=deps.sender_verification,
            department=fallback.address,
            subject=sanitize_header(f"Do weryfikacji: {deps.original_message}", max_length=80),
            reason=reason,
            original_message=deps.original_message,
            request_id=deps.request_id,
            ticket_code=deps.ticket_code,
            routed_by="fallback",
            model_name=self._runtime.model_name,
            prompt_version=self._runtime.prompt_version,
            mail_settings=self._runtime.mail_settings,
            needs_review=True,
            feedback_url=deps.feedback_url,
        )
        record = SentEmail(
            ticket_id=deps.ticket_id,
            department=fallback.address,
            chosen_department=fallback.address,
            reply_to=deps.sender_email,
            subject=str(email["Subject"]),
            reason=reason,
            message_id=str(email["Message-ID"]),
            routed_by="fallback",
            confidence=None,
            needs_review=True,
            delivery="queued",
        )
        try:
            delivery = await self._gateway.submit(email, record)
        except MailGatewayError as exc:
            raise StorageUnavailableError() from exc
        log_event(
            logger, logging.WARNING, "routing.fallback", cause=failure, message_id=record.message_id
        )
        return SentEmail(**{**asdict(record), "delivery": delivery})

    def _completed(
        self,
        request: RoutingRequest,
        sent: SentEmail,
        diagnostics: AgentDiagnostics,
        warnings: tuple[str, ...],
    ) -> RoutingOutcome:
        metrics.ROUTED.labels(sent.routed_by, sent.department).inc()
        metrics.AGENT_DURATION.observe(diagnostics.duration_ms / 1000)
        metrics.LLM_TOKENS.labels("input").inc(diagnostics.input_tokens)
        metrics.LLM_TOKENS.labels("output").inc(diagnostics.output_tokens)
        metrics.AGENT_RETRIES.inc(diagnostics.retries)
        log_event(
            logger,
            logging.INFO,
            "routing.completed",
            ticket=request.ticket_code,
            department=sent.department,
            routed_by=sent.routed_by,
            confidence=sent.confidence,
            delivery=sent.delivery,
            message_id=sent.message_id,
            warnings=list(warnings),
            **asdict(diagnostics),
        )
        return RoutingOutcome(request, sent, diagnostics, warnings)

    def _diagnostics(
        self,
        messages: Sequence[ModelMessage],
        deps: RoutingDeps,
        started: float,
        queue_wait_ms: int,
    ) -> AgentDiagnostics:
        llm_requests, retries, input_tokens, output_tokens, max_input = summarize_messages(messages)
        limit = self._settings.ollama_context_length * self._settings.context_warning_ratio
        if max_input > limit:
            metrics.CONTEXT_NEAR_LIMIT.inc()
            log_event(
                logger,
                logging.WARNING,
                "llm.context_near_limit",
                input_tokens=max_input,
                context_length=self._settings.ollama_context_length,
            )
        return AgentDiagnostics(
            model=self._runtime.model_name,
            agent_mode=self._agent.mode,
            prompt_version=self._agent.prompt_version,
            llm_requests=llm_requests,
            tool_calls=deps.tool_attempts,
            tool_duplicates=deps.tool_duplicates,
            retries=retries,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
            queue_wait_ms=queue_wait_ms,
        )

    @staticmethod
    def _failure_error(failure: str) -> AppError:
        if failure == FAILURE_LLM_UNAVAILABLE:
            return LlmUnavailableError("The LLM backend did not respond and fallback is disabled.")
        if failure == FAILURE_LLM_MISCONFIGURED:
            return LlmMisconfiguredError("The LLM backend rejected the request; check the model.")
        if failure == FAILURE_TIMEOUT:
            return RoutingTimeoutError("The agent did not finish in time and fallback is disabled.")
        return AgentFailedError(f"The agent did not route the message ({failure}).")

    def _log_received(self, request: RoutingRequest) -> None:
        fields: dict[str, object] = {
            "ticket": request.ticket_code,
            "message_chars": len(request.message),
            "message_sha256": hashlib.sha256(request.message.encode()).hexdigest()[:16],
            "sender_domain": request.sender_email.rsplit("@", 1)[-1],
            "sender_verification": request.sender_verification,
        }
        if self._settings.log_message_content:
            fields["message"] = request.message  # safe with log_event (no LogRecord collision)
        log_event(logger, logging.INFO, "routing.started", **fields)
